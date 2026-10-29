# GNSS Tracker — RTK / CLAS

Raspberry PiでGNSSの位置ログを記録し、SORACOM Harvest Dataへ送る実験用プログラムです。2026年10月22日の車載実験で使用した共通処理を、独立したリポジトリとして切り出しました。個人で開発した非公式の実験コードです。

- **RTK**: Quectel QLM29HにNTRIPの補正データを渡し、NMEAを記録。
- **CLAS**: F9PX1 / ZED-F9PとD9CX1 / NEO-D9Cの構成から出力されるNMEAを記録。CLASの測位演算・補強処理は受信機側で行います。
- **共通**: GNSSのUTCに合わせた5秒間隔の記録、同時刻のRMC/GGAの結合、SQLiteへの保存、HTTP再送、安定した観測ID。

受信機・アンテナ・ファームウェアの設定は別途必要です。`CLAS`というラベルやFIX表示だけで、特定の補強情報の実使用や位置精度を保証するものではありません。

## ファイルと実装

| ファイル | 役割 |
| --- | --- |
| [rtk_nmea_unified.py](rtk_nmea_unified.py) | RTK側の起動 |
| [gnss_sender.py](gnss_sender.py) | CLAS側の起動 |
| [measurement.py](gnss_telemetry/measurement.py) | NMEAの結合・時刻・有効性・観測ID |
| [delivery.py](gnss_telemetry/delivery.py) | 永続キュー・HTTP送信・再送・隔離 |
| [ntrip.py](gnss_telemetry/ntrip.py) | NTRIP接続・GGA送信・補正転送 |
| [app.py](gnss_telemetry/app.py) | 引数・シリアル接続・状態記録・終了処理 |
| [実装詳細](docs/implementation.md) | 記録項目、容量、再送条件、検証と読み戻し |

元の共通処理と2つの起動スクリプトをそのまま収録しています。実機の認証ファイル、SIM情報、測位ログ、未送信キューは含めていません。テストの座標・日時は架空の固定値です。

## 前提

LinuxのRaspberry Pi、Python 3.10以降、GNSS受信機のUSBシリアル、LTEモデムとSORACOM回線、対象SIMのHarvest Data有効化を用意します。実行ユーザーにシリアルポートへのアクセス権が必要です。`fcntl`とシリアルの排他制御を使うため、Windowsを実行対象にはしていません。

RTK側は利用権のあるNTRIPサービスの接続情報も必要です。HTTP送信先は既定で`http://harvest.soracom.io`です。SORACOM回線を通る経路を確認してください。CLAS側のNMEA読み取りにはNTRIPは使いません。

## セットアップ

```sh
git clone https://github.com/takao2704/gnss-tracker.git
cd gnss-tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

`requirements.txt`は依存パッケージの指定、`requirements.lock`は元の実機配置時に記録した固定バージョンです。依存パッケージの配布状況に応じて導入環境を確認してください。

## 設定とdry-run

USBポートは機器ごとに確認し、本番では`/dev/serial/by-id/…`の固定識別パスを使います。2台で同じ走行IDを指定し、別の走行ではIDを変えます。同じ受信機種を複数台使う場合、観測IDは機器固有IDを含まないため、走行ID等を分けて衝突を避けてください。

```sh
# CLAS: 読み取りとJSON表示。HTTP送信・受信機設定変更なし
.venv/bin/python gnss_sender.py --port /dev/ttyACM0 --baud 9600 --run example-drive-001 --dry-run
# RTK: 読み取りとJSON表示。NTRIP接続・HTTP送信・受信機設定変更なし
.venv/bin/python rtk_nmea_unified.py --port /dev/ttyUSB0 --baud 115200 --run example-drive-001 --dry-run
```

`--dry-run`でもシリアルポートを開き、ローカルの状態ディレクトリにロック・状態ファイルを作ります。既存サービスと同じポートを同時に開かないでください。JSON表示には測位位置が含まれます。

通常実行の共通設定は次の環境変数でも指定できます。

| 環境変数 | 内容 |
| --- | --- |
| `SERIAL_PORT` / `SERIAL_BAUD` | シリアルポートと速度 |
| `GNSS_RUN_ID` | 実験・走行ID。必須 |
| `GNSS_INTERVAL` | 保存間隔。既定5秒、60の約数 |
| `GNSS_SPOOL_DIR` | 状態・未送信キューの保存ディレクトリ |
| `UNIFIED_ENDPOINT` | HTTP送信先。既定はHarvest専用エンドポイント |
| `NTRIP_HOST` / `NTRIP_PORT` / `NTRIP_MOUNT` | RTK側の接続先。既定はQuectelのホスト・2101・AUTO。利用契約に合わせて指定 |
| `NTRIP_USER` / `NTRIP_PASS` | RTK側の認証情報 |
| `QLM29H_DR_STATE` | RTK側のDR要求設定。`off` / `on` / `unchanged` |

NTRIPの認証値は機器側のアクセス権を制限した設定ファイルから環境変数へ読み込みます。リポジトリやコマンドライン引数には保存しないでください。この実装はNTRIP v1のTCP接続で、TLS・chunked応答には対応しません。

## 通常実行

```sh
# CLAS
.venv/bin/python gnss_sender.py --port /dev/ttyACM0 --baud 9600 --run example-drive-001
# RTK（NTRIP環境変数を設定した後）
.venv/bin/python rtk_nmea_unified.py --port /dev/ttyUSB0 --baud 115200 --run example-drive-001 --dr-state off
```

通常実行は実データをHarvestへ送ります。RTK側は受信機へRTK・DRの設定コマンドを送り、NTRIP接続先へ認証情報と概略位置のGGAを送信します。`--no-ntrip`はNTRIP接続だけを止める指定で、受信機設定やHarvest送信を止めるものではありません。

## 自動起動

systemdの例を[docs/systemd.md](docs/systemd.md)に載せています。既存の送信サービスがある場合は、同時起動を避け、未送信キューと設定を退避してから切り替えます。既存キューの保存先を変えると、以前の送信待ちデータは新しいサービスから見えなくなります。

## 保存・再送のポイント

観測をSQLiteに保存してからHTTP送信し、成功応答を受け取ってから削除します。一時的な失敗は再送し、要求内容の問題などは隔離します。再送時の本文と観測IDは変えません。

サーバーで保存後に応答が失われると重複が発生し得るため、読み出し側でも`event_id`を使って重複を除きます。5万件またはJSON合計100MiBの上限を超えると古い記録を捨てる実装で、無期限の配送保証ではありません。送信済みの全履歴を端末に残す場合は、別途アーカイブが必要です。

`status.json`とサービスログで、未送信件数・隔離件数・容量超過の削除件数を確認します。30秒間有効なRMC/GGAが来ない場合やUSB障害は終了条件となり、systemdに再起動を任せます。

## 検証範囲

18件の自動テストは架空のNMEAとHTTPモックを使います。測位日時の結合・欠測・重複ID・再起動後のキュー・再送・隔離・NTRIPヘッダー処理を確認します。実機の測位精度、LTE経路、補強情報の適用を確認するテストではありません。

元の車載実験では両機のサービス再起動後に未送信IDを保持し、送信先復旧後にHarvestから読み戻しました。公開用に切り出した環境での確認は[検証記録](docs/validation.md)に記載します。物理電源断・SDカード故障・長期間の無人運用は保証していません。
