# Raspberry Pi 置き配監視システム

玄関カメラの画像を比較し、置き配の検出と撤去を判定するシステムです。

人の出入り、扉の開閉、夕方や雨天による明るさの変化を除外し、約100秒後に再判定することで誤動作を減らします。

## 主な機能

- 置き配の検出と撤去判定
- 人の動きと扉開閉の除外
- 照度に合う基準画像の自動選択
- MQTTによる緑LEDの点灯・消灯
- LINEによる検出・撤去通知
- 検出写真のDropbox自動保存
- 写真保存完了のLINE通知

## 実証済み設定

- 検出しきい値：4.0%
- 撤去しきい値：2.0%
- 扉変化しきい値：50.0%
- 静止確認：5秒
- 仮判定：20秒
- 再判定待機：60秒
- 再確認：20秒
- 監視時間：07:00～18:00

## ファイル構成

- `delivery_monitor_v2.py`：本番の置き配監視
- `photo_dropbox_uploader.py`：写真のDropbox保存とMQTT通知
- `compare_images.py`：荷物あり・なし画像の比較
- `check_door_area.py`：扉領域の変化率確認
- `requirements.txt`：必要なPythonライブラリ
- `systemd/`：自動起動サービスの見本

## 必要環境

- Raspberry Pi OS 64-bit
- Python 3.11
- USB Webカメラ
- Mosquitto MQTTブローカー
- rclone・Dropbox
- LINE Messaging API
- Pico W（LED表示用）

## 導入

1. `python3 -m venv venv`
2. `source venv/bin/activate`
3. `pip install -r requirements.txt`

## 安全上の注意

- `.env`やアクセストークンはGitHubへ登録しないでください。
- 玄関画像と配達物写真は公開しないでください。
- Dropbox写真は非公開保存を基本とします。
- カメラ映像をインターネットへ直接公開しないでください。
- 設置環境に合わせて領域としきい値を調整してください。
