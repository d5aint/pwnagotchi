import logging
import os
import shutil
import time
import zipfile
from threading import Lock

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from pydrive2.files import ApiRequestError

import pwnagotchi
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile


class GdriveSync(plugins.Plugin):
    __author__ = '@jayofelony & Moist'
    __version__ = '1.4'
    __license__ = 'GPL3'
    __description__ = 'Backup pwnagotchi files to Google Drive.'

    def __init__(self):
        self.options = dict()
        self.lock = Lock()
        self.internet = False
        self.ready = False
        self.status = StatusFile('/root/.gdrive-backup')
        self.backup = True
        self.drive = None
        self.backupfiles = [
            '/root/brain.json',
            '/root/.api-report.json',
            '/home/pi/handshakes',
            '/root/peers',
            '/etc/pwnagotchi',
            '/etc/profile',  # Fixed path typo (.etc -> /etc)
            '/usr/local/share/pwnagotchi/custom-plugins',
            '/boot/firmware/config.txt',
            '/boot/firmware/cmdline.txt'
        ]

    def on_loaded(self):
        """
        Called when the plugin is loaded
        """
        # Check if secrets file exists and is not empty
        secrets_path = "/root/client_secrets.json"
        if not os.path.exists(secrets_path) or os.stat(secrets_path).st_size == 0:
            logging.error("[gdrivesync] /root/client_secrets.json missing or empty.")
            return

        if not os.path.exists("/root/.gdrive-backup"):
            self.backup = False

        try:
            gauth = GoogleAuth(settings_file="/root/settings.yaml")
            gauth.LoadCredentialsFile("/root/credentials.json")
            
            if gauth.credentials is None:
                gauth.LocalWebserverAuth()
            elif gauth.access_token_expired:
                gauth.Refresh()
            
            gauth.SaveCredentialsFile("/root/credentials.json")
            gauth.Authorize()

            self.drive = GoogleDrive(gauth)
            
            backup_folder_name = self.options.get('backup_folder', 'Pwnagotchi_Backup')

            # Initial Backup / Restore Logic
            if not self.backup:
                backup_folder_id = self.create_folder_if_not_exists(backup_folder_name)
                
                query = (f"'{backup_folder_id}' in parents and "
                         f"mimeType = 'application/vnd.google-apps.folder' and trashed=false")
                
                backup_list = self.drive.ListFile({'q': query}).GetList()

                if not backup_list:
                    # No backup found on Drive, create one
                    user_files = self.options.get('backupfiles', [])
                    if user_files:
                        self.backupfiles.extend(user_files)
                    
                    self.backup_files(self.backupfiles, '/home/pi/backup')
                    
                    zip_path = self.create_zip_archive('/home/pi/backup', '/home/pi/backup.zip')
                    
                    self.upload_to_gdrive(zip_path, backup_folder_id)
                    self.backup = True
                    self.status.update()
                
                # Restore logic
                local_zip_path = '/home/pi/backup.zip'
                zip_file_id = self.get_latest_backup_file_id(backup_folder_name)
                
                if zip_file_id:
                    zip_file = self.drive.CreateFile({'id': zip_file_id})
                    zip_file.GetContentFile(local_zip_path)
                    
                    logging.info("[gdrivesync] Downloaded backup.zip from Google Drive")
                    
                    with zipfile.ZipFile(local_zip_path, 'r') as zip_ref:
                        zip_ref.extractall('/')
                        
                    self.status.update()
                    
                    # Cleanup
                    if os.path.exists("/home/pi/backup"):
                        shutil.rmtree("/home/pi/backup")
                    if os.path.exists(local_zip_path):
                        os.remove(local_zip_path)
                        
                    self.ready = True
                    logging.info("[gdrivesync] Restore complete. Restarting...")
                    pwnagotchi.restart("AUTO")
                    return

            self.ready = True
            logging.info("[gdrivesync] loaded")
            
        except Exception as e:
            logging.error(f"[gdrivesync] Init Error: {e}")
            self.ready = False

    def get_latest_backup_file_id(self, backup_folder_name):
        folder_id = self.get_folder_id_by_name(self.drive, backup_folder_name)
        if not folder_id:
            return None
            
        query = f"'{folder_id}' in parents and trashed=false"
        file_list = self.drive.ListFile({'q': query}).GetList()

        if file_list:
            latest = max(file_list, key=lambda f: f['createdDate'])
            return latest['id']
        return None

    def get_folder_id_by_name(self, drive, folder_name, parent_id=None):
        query = "mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        file_list = drive.ListFile({'q': query}).GetList()
        for file in file_list:
            if file['title'] == folder_name:
                return file['id']
        return None

    def create_folder_if_not_exists(self, folder_name):
        folder_id = self.get_folder_id_by_name(self.drive, folder_name)
        if folder_id is None:
            folder = self.drive.CreateFile({
                'title': folder_name, 
                'mimeType': 'application/vnd.google-apps.folder'
            })
            folder.Upload()
            folder_id = folder['id']
            logging.info(f"[gdrivesync] Created folder '{folder_name}' ID: {folder_id}")
        return folder_id

    def create_zip_archive(self, source_dir, output_filename):
        with zipfile.ZipFile(output_filename, 'w') as zip_ref:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    zip_ref.write(file_path, arcname=arcname)
        return output_filename

    def on_unload(self, ui):
        logging.info("[gdrivesync] unloaded")

    def on_internet_available(self, agent):
        self.internet = True

    def on_handshake(self, agent, filename, access_point, client_station):
        if not self.ready or not self.internet:
            return
            
        if self.lock.locked():
            return
            
        with self.lock:
            interval = self.options.get('interval', 1)
            if self.status.newer_then_hours(interval):
                logging.debug(f"[gdrivesync] Last backup < {interval} hours ago.")
                return

            logging.info("[gdrivesync] Handshake captured, starting backup...")
            
            user_files = self.options.get('backupfiles', [])
            if user_files:
                self.backupfiles.extend(user_files)
                
            self.backup_files(self.backupfiles, '/home/pi/backup')
            
            zip_path = self.create_zip_archive('/home/pi/backup', '/home/pi/backup.zip')
            
            folder_name = self.options.get('backup_folder', 'Pwnagotchi_Backup')
            folder_id = self.get_folder_id_by_name(self.drive, folder_name)
            
            self.upload_to_gdrive(zip_path, folder_id)
            
            if hasattr(agent.view(), 'on_uploading'):
                agent.view().on_uploading("Google Drive")

            # Cleanup
            if os.path.exists(zip_path):
                os.remove(zip_path)
            if os.path.exists("/home/pi/backup"):
                shutil.rmtree("/home/pi/backup")
                
            self.status.update()
            agent.view().update(force=True, new_data={'status': 'Backed up to GDrive'})

    def backup_files(self, paths, dest_path):
        for src in paths:
            try:
                if os.path.exists(src):
                    rel_path = os.path.relpath(src, '/')
                    dest = os.path.join(dest_path, rel_path)

                    if os.path.isfile(src):
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        if os.path.exists(dest):
                            os.remove(dest)
                        shutil.copy2(src, dest)
                    elif os.path.isdir(src):
                        if os.path.exists(dest):
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
            except Exception as e:
                logging.error(f"[gdrivesync] Backup error for {src}: {e}")

    def upload_to_gdrive(self, file_path, parent_id):
        try:
            file_meta = {
                'title': 'backup.zip', 
                'parents': [{'id': parent_id}]
            }
            gfile = self.drive.CreateFile(file_meta)
            gfile.SetContentFile(file_path)
            gfile.Upload()
            logging.info("[gdrivesync] Upload successful")
        except ApiRequestError as e:
            self.handle_upload_error(e, file_path, parent_id)
        except Exception as e:
            logging.error(f"[gdrivesync] Upload error: {e}")

    def handle_upload_error(self, error, file_path, parent_id):
        if 'Rate Limit Exceeded' in str(error):
            logging.warning("[gdrivesync] Rate limit. Retrying in 100s...")
            time.sleep(100)
            self.upload_to_gdrive(file_path, parent_id)
        else:
            logging.error(f"[gdrivesync] API Error: {error}")
