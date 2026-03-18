import asyncio
from document_storage import DocumentStorageSettings, DocumentStorageService

service = DocumentStorageService(DocumentStorageSettings.from_env())
service.run_migrations()
print("Migrations run successfully.")
