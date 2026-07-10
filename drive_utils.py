import io
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

class GoogleDriveManager:
    def __init__(self):
        scopes = ['https://www.googleapis.com/auth/drive.readonly']
        self.cache_dir = "temp_cache"
        self.max_cache_size = 100 * 1024 * 1024  # 100 MB Limit
        os.makedirs(self.cache_dir, exist_ok=True)
        
        env_creds = os.getenv('GOOGLE_CREDS_JSON')
        if env_creds:
            try:
                creds_dict = json.loads(env_creds)
                self.creds = service_account.Credentials.from_service_account_info(
                    creds_dict, scopes=scopes
                )
            except Exception as e:
                raise RuntimeError(f"Failed to parse GOOGLE_CREDS_JSON: {e}")
        elif os.path.exists('service_account.json'):
            self.creds = service_account.Credentials.from_service_account_file(
                'service_account.json', scopes=scopes
            )
        else:
            raise FileNotFoundError("Critical Error: No credentials found.")
            
        self.service = build('drive', 'v3', credentials=self.creds)

    def list_audio_files(self, folder_id):
        try:
            all_files = []
            
            def scan_folder(current_id):
                # Fetch items in the folder
                query = f"'{current_id}' in parents and trashed = false"
                results = self.service.files().list(
                    q=query, spaces='drive', fields="files(id, name, mimeType)"
                ).execute()
                
                items = results.get('files', [])
                for item in items:
                    # If it's a folder, look inside it
                    if item.get('mimeType') == 'application/vnd.google-apps.folder':
                        scan_folder(item['id'])
                    # Only add if it is an audio file
                    elif item.get('mimeType', '').startswith('audio/') or item.get('name', '').endswith(('.mp3', '.m4a', '.wav')):
                        all_files.append(item)
            
            scan_folder(folder_id)
            # Sort files by name for a consistent playlist
            return sorted(all_files, key=lambda x: x.get('name', ''))
            
        except Exception as e:
            print(f"Error listing files from Drive: {e}")
            return []

    def _manage_cache_size(self, new_file_size):
        if not os.path.exists(self.cache_dir): return
        total_size = sum(os.path.getsize(os.path.join(self.cache_dir, f)) for f in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, f)))
        while total_size + new_file_size > self.max_cache_size:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, f))]
            if not files: break
            oldest_file = min(files, key=os.path.getatime)
            total_size -= os.path.getsize(oldest_file)
            os.remove(oldest_file)

    def get_or_download_track(self, file_id, file_name):
        local_path = os.path.join(self.cache_dir, f"{file_id}.mp3")
        if os.path.exists(local_path):
            return local_path, True
        try:
            file_metadata = self.service.files().get(fileId=file_id, fields="size").execute()
            self._manage_cache_size(int(file_metadata.get("size", 0)))
            request = self.service.files().get_media(fileId=file_id)
            with open(local_path, "wb") as file_stream:
                downloader = MediaIoBaseDownload(file_stream, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
            return local_path, True
        except Exception as e:
            return None, False