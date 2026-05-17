import json
import logging
from datetime import datetime
import pytz
from app.core.db import db

log = logging.getLogger(__name__)

# To break circular import if need gemini, we can inject model_ia into functions or do importing properly in service layer. 
# Or we just do DB interaction here, leaving AI generation out.
# Let's extract only DB interactions to ConversationModel, the AI Generation goes to gemini_service.py

class ConversationRepository:
    @staticmethod
    def append_message_to_db(contact_id, role, text, message_id=None):
        if db.conversation_collection is None:
            return False
        try:
            tz = pytz.timezone('America/Sao_Paulo')
            now = datetime.now(tz)
            entry = {'role': role, 'text': text, 'ts': now.isoformat()}
            if message_id:
                entry['msg_id'] = message_id

            db.conversation_collection.update_one(
                {'_id': contact_id},
                {'$push': {'history': entry}, '$setOnInsert': {'created_at': now}},
                upsert=True
            )
            return True
        except Exception as e:
            log.error(f"❌ Erro ao append_message_to_db: {e}")
            return False

    @staticmethod
    def load_conversation_from_db(contact_id):
        if db.conversation_collection is None: return None
        try:
            result = db.conversation_collection.find_one({'_id': contact_id})
            if result:
                history = result.get('history', [])
                history_filtered = [msg for msg in history if not msg.get('text', '').strip().startswith("A data e hora atuais são:")]
                history_sorted = sorted(history_filtered, key=lambda m: m.get('ts', ''))
                result['history'] = history_sorted
                return result
        except Exception as e:
            log.error(f"❌ Erro ao carregar conversa do MongoDB para {contact_id}: {e}")
        return None

    @staticmethod
    def save_metadata(contact_id, sender_name, customer_name, tokens_in, tokens_out, status_calculado):
        if db.conversation_collection is None: return
        try:
            doc_atual = db.conversation_collection.find_one({'_id': contact_id})
            status_anterior = doc_atual.get('conversation_status', 'andamento') if doc_atual else 'andamento'

            total_combined = tokens_in + tokens_out
            update_payload = {
                'sender_name': sender_name,
                'last_interaction': datetime.now(),
                'conversation_status': status_calculado,
            }

            should_reset_stage = False
            if status_calculado == 'stand_by':
                update_payload['followup_stage'] = 99 
                should_reset_stage = False
            elif status_calculado == 'andamento':
                should_reset_stage = True
            elif status_calculado != status_anterior:
                should_reset_stage = True
            
            if should_reset_stage:
                update_payload['followup_stage'] = 0

            if customer_name:
                update_payload['customer_name'] = customer_name

            db.conversation_collection.update_one(
                {'_id': contact_id},
                {
                    '$set': update_payload,
                    '$inc': {
                        'total_tokens_consumed': total_combined,
                        'tokens_input': tokens_in,
                        'tokens_output': tokens_out
                    } 
                },
                upsert=True
            )
        except Exception as e:
            log.error(f"❌ Erro ao salvar metadados: {e}")

    @staticmethod
    def update_profiler(contact_id, novo_perfil_json, novo_checkpoint_ts, in_tok, out_tok):
        if db.conversation_collection is None: return
        db.conversation_collection.update_one(
            {'_id': contact_id},
            {
                '$set': {
                    'client_profile': novo_perfil_json,
                    'profiler_last_ts': novo_checkpoint_ts
                },
                '$inc': {
                    'total_tokens_consumed': in_tok + out_tok,
                    'tokens_input': in_tok,
                    'tokens_output': out_tok
                }
            }
        )
