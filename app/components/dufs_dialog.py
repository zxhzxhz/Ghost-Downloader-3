# app/components/dufs_dialog.py

import os
import json
from urllib.parse import urljoin, unquote, urlparse, urlunparse
import urllib.request
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QPushButton, QVBoxLayout, QHBoxLayout
from qfluentwidgets import FluentIcon as FIF, SubtitleLabel, MessageBoxBase, LineEdit, InfoBar, InfoBarPosition
from loguru import logger

from ..common.methods import addDownloadTask
from .select_folder_setting_card import SelectFolderSettingCard
from ..common.config import cfg

class ParseDufsThread(QThread):
    """ 用於解析 dufs 網頁的執行緒 """
    finished_signal = Signal(str, list)
    error_signal = Signal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.initial_url = url.strip()
        # 確保 initial_url 以斜線結尾，方便後續路徑拼接
        if not self.initial_url.endswith('/'):
            self.initial_url += '/'
            
        self.files_to_process = [{'type': 'Dir', 'url': self.initial_url}]
        self.processed_urls = set()
        self.headers = {'User-Agent': 'Mozilla/5.0'}
        self.running = True

    def stop(self):
        """ 停止執行緒 """
        logger.info("ParseDufsThread stop signal received.")
        self.running = False

    def run(self):
        logger.info(f"Starting Dufs parsing for base URL: {self.initial_url}")
        try:
            all_files = []
            
            # 首先獲取根目錄的 JSON 以取得 href
            initial_json_url = f"{self.initial_url}?json"
            logger.debug(f"Fetching initial JSON from: {initial_json_url}")
            req = urllib.request.Request(initial_json_url, headers=self.headers)
            with urllib.request.urlopen(req) as response:
                if response.status != 200:
                    raise Exception(f"請求根目錄失敗，狀態碼: {response.status}")
                initial_data = json.loads(response.read().decode('utf-8'))
            
            root_folder_name = unquote(initial_data.get('href', '/')).strip('/')
            if not root_folder_name: # 如果根目錄是'/'，則從URL中提取最後一部分
                 root_folder_name = unquote(urlparse(self.initial_url).path.strip('/').split('/')[-1])
            logger.info(f"Determined root folder name: {root_folder_name}")

            while self.files_to_process:
                if not self.running:
                    logger.warning("Parsing interrupted by user.")
                    return

                current_item = self.files_to_process.pop(0)
                current_url = current_item['url']

                if current_url in self.processed_urls:
                    continue
                self.processed_urls.add(current_url)

                json_url = f"{current_url}?json"
                logger.debug(f"Processing directory: {json_url}")
                
                req = urllib.request.Request(json_url, headers=self.headers)
                with urllib.request.urlopen(req) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to fetch {json_url}, status: {response.status}. Skipping.")
                        continue
                    content = response.read().decode('utf-8')
                    data = json.loads(content)

                for path_info in data.get('paths', []):
                    if not self.running:
                        logger.warning("Parsing interrupted by user inside loop.")
                        return
                        
                    name = path_info['name']
                    encoded_name = urllib.parse.quote(name)
                    full_url = urljoin(current_url, encoded_name)
                    
                    relative_path = unquote(full_url.replace(self.initial_url, ''))
                    
                    if path_info['path_type'] == 'Dir':
                        logger.info(f"Found directory: {name}")
                        self.files_to_process.append({'type': 'Dir', 'url': f"{full_url}/"})
                    else:
                        logger.info(f"Found file: {name} at path: {relative_path}")
                        all_files.append({'name': name, 'url': full_url, 'path': relative_path})
            
            if self.running:
                self.finished_signal.emit(root_folder_name, all_files)
                logger.success("Dufs parsing finished successfully.")
        except Exception as e:
            logger.error(f"An error occurred during parsing: {e}")
            if self.running:
                self.error_signal.emit(str(e))


class DufsDialog(MessageBoxBase):
    """ Dufs 解析與下載對話視窗 """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("從 dufs 頁面新增下載任務")
        self.widget.setFixedSize(600, 700)
        self.root_folder = ""
        self.parse_thread = None

        self.titleLabel = SubtitleLabel("Dufs 網頁連結", self.widget)
        self.urlLineEdit = LineEdit(self.widget)
        self.urlLineEdit.setPlaceholderText("請輸入 dufs 網頁的 URL")
        self.parseButton = QPushButton("解析", self.widget)

        self.fileTree = QTreeWidget(self.widget)
        self.fileTree.setHeaderLabels(["檔案", "路徑"])
        self.fileTree.setColumnWidth(0, 300)

        self.downloadFolderCard = SelectFolderSettingCard(cfg.downloadFolder, cfg.historyDownloadFolder, self.widget)

        self.h_layout = QHBoxLayout()
        self.h_layout.addWidget(self.urlLineEdit)
        self.h_layout.addWidget(self.parseButton)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(self.h_layout)
        self.viewLayout.addWidget(self.fileTree)
        self.viewLayout.addWidget(self.downloadFolderCard)
        
        # --- UI Refactor ---
        self.yesButton.setText("下載")
        self.cancelButton.setText("取消")
        
        self.yesButton.clicked.disconnect() # 斷開預設的 accept()
        self.yesButton.clicked.connect(self.start_download)
        self.parseButton.clicked.connect(self.start_parsing)

    def start_parsing(self):
        url = self.urlLineEdit.text()
        if not url:
            return

        # 如果存在正在運行的執行緒，先停止它
        if self.parse_thread and self.parse_thread.isRunning():
            self.parse_thread.stop()
            self.parse_thread.wait()

        self.parse_thread = ParseDufsThread(url)
        self.parse_thread.finished_signal.connect(self.on_parsing_finished)
        self.parse_thread.error_signal.connect(self.on_parsing_error)
        self.parse_thread.start()
        self.parseButton.setText("解析中...")
        self.parseButton.setEnabled(False)
        self.yesButton.setEnabled(False)

    def on_parsing_finished(self, root_folder, file_list):
        if not self.isVisible(): return
        self.root_folder = root_folder
        self.fileTree.clear()
        for file_info in file_list:
            item = QTreeWidgetItem(self.fileTree, [file_info['name'], file_info['path']])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked)
            item.setData(0, Qt.UserRole, file_info['url'])
        self.parseButton.setText("解析")
        self.parseButton.setEnabled(True)
        self.yesButton.setEnabled(True)

    def on_parsing_error(self, error_message):
        if not self.isVisible(): return
        InfoBar.error(
            title="錯誤",
            content=f"解析失敗: {error_message}",
            position=InfoBarPosition.TOP,
            parent=self,
            duration=3000,
        )
        self.parseButton.setText("解析")
        self.parseButton.setEnabled(True)
        self.yesButton.setEnabled(True)

    def start_download(self):
        download_path = self.downloadFolderCard.editableComboBox.currentText()
        if download_path == self.downloadFolderCard.editableComboBox.defaultText:
            download_path = self.downloadFolderCard.editableComboBox.default
        
        task_root_path = os.path.join(download_path, self.root_folder)
        if not os.path.exists(task_root_path):
            os.makedirs(task_root_path)
            logger.info(f"Created root download directory: {task_root_path}")

        root_item = self.fileTree.invisibleRootItem()
        for i in range(root_item.childCount()):
            item = root_item.child(i)
            if item.checkState(0) == Qt.Checked:
                file_name = item.text(0)
                relative_path = item.text(1)
                url = item.data(0, Qt.UserRole)

                local_file_path = os.path.join(task_root_path, os.path.dirname(relative_path))
                if not os.path.exists(local_file_path):
                    os.makedirs(local_file_path)
                
                logger.info(f"Adding download task: URL='{url}', FileName='{file_name}', Path='{local_file_path}'")
                addDownloadTask(url=url, fileName=file_name, filePath=local_file_path, notCreateHistoryFile=True)

        self.accept() # 使用 accept() 關閉對話框

    def closeEvent(self, event):
        if self.parse_thread and self.parse_thread.isRunning():
            self.parse_thread.stop()
            self.parse_thread.wait() # 等待執行緒結束
        super().closeEvent(event)