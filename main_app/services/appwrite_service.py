import os
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID

class AppwriteService:
    def __init__(self):
        self.client = Client()
        self.client.set_endpoint(os.getenv('APPWRITE_ENDPOINT', 'http://appwrite.misproject.us.ci/v1'))
        self.client.set_project(os.getenv('APPWRITE_PROJECT_ID', '6a44de590010fa46afbd'))
        self.client.set_key(os.getenv('APPWRITE_API_KEY', ''))
        self.databases = Databases(self.client)
        self.db_id = os.getenv('APPWRITE_DB_ID', 'dating_db')
        self.collection_id = 'chat_messages'

    def save_chat_message(self, sender_id: str, receiver_id: str, room_id: str, content: str, is_system: bool = False, triggered_by_msg_id: str | None = None) -> dict:
        try:
            data = {
                "sender_id": sender_id,
                "receiver_id": receiver_id,
                "room_id": room_id,
                "content": content,
                "type": "text",
                "is_unsent": False
            }
            if triggered_by_msg_id is not None:
                data["triggered_by_msg_id"] = triggered_by_msg_id
            doc = self.databases.create_document(
                database_id=self.db_id,
                collection_id=self.collection_id,
                document_id=ID.unique(),
                data=data
            )
            return doc
          
        except Exception as e:
            print(f"❌ Appwrite save_chat_message error: {e}")
            raise e

appwrite_srv = AppwriteService()
