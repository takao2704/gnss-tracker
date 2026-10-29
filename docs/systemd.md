# systemdによる自動起動

以下はユーザー`pi`、配置先`/home/pi/gnss-tracker`の例です。実際のユーザー名、配置先、シリアルアクセス権に合わせて変更してください。既存サービスの上書きや停止は自動では行いません。

## 事前確認

1. 既存サービス・ポート・未送信キューを確認し、設定とデータを退避します。
2. 同じ受信機を読む古いサービスを停止します。未送信データが残っている場合は先に送信を完了させるか、互換性を確認して同じ保存先を引き継ぎます。
3. 新プログラムのdry-runと通常実行を手動で確認します。

## サービスファイル

`/etc/systemd/system/gnss-tracker@.service`:

```ini
[Unit]
Description=GNSS recorder %i
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/gnss-tracker
EnvironmentFile=/etc/gnss-tracker/%i.env
ExecStart=/home/pi/gnss-tracker/.venv/bin/python /home/pi/gnss-tracker/%i.py
Restart=always
RestartSec=5
TimeoutStopSec=35
UMask=0077

[Install]
WantedBy=multi-user.target
```

RTKは`/etc/gnss-tracker/rtk_nmea_unified.env`、CLASは`/etc/gnss-tracker/gnss_sender.env`に設定します。root所有、モード600にし、リポジトリへコピーしないでください。systemdが設定を読み、`pi`で起動する子プロセスへ渡します。

```ini
# CLASの例。USB識別パスは実機のものに置換
SERIAL_PORT=/dev/serial/by-id/REPLACE_WITH_DEVICE_ID
SERIAL_BAUD=9600
GNSS_RUN_ID=example-drive-001
GNSS_INTERVAL=5
GNSS_SPOOL_DIR=/home/pi/.local/state/gnss-comparison/clas
```

RTK側は速度を115200、保存先の末尾を`rtk`にし、`NTRIP_HOST`、`NTRIP_PORT`、`NTRIP_MOUNT`、`NTRIP_USER`、`NTRIP_PASS`、`QLM29H_DR_STATE=off`を設定します。環境ファイルはシェルスクリプトではないため、`export`は付けません。

## 起動と確認

RTK側の例です。CLAS側はインスタンス名を`gnss_sender`に置き換えます。

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now gnss-tracker@rtk_nmea_unified.service
systemctl status gnss-tracker@rtk_nmea_unified.service
journalctl -u gnss-tracker@rtk_nmea_unified.service -n 30 --no-pager
```

`START`だけで判断せず、RMC/GGAの記録、RTK側のRTCM受信、未送信件数、Harvestでの測位時刻と観測IDを確認します。OS再起動後にも同じ確認を行います。このテンプレートの新規インストールとOS再起動は公開作業では実行していません。

## 更新・停止

同じキューを読む新旧プロセスを同時に実行しません。停止してから更新し、環境ファイルとキューの保存先を維持して再起動します。切り戻す場合は旧バージョンのキュー互換性も確認します。

```sh
sudo systemctl stop gnss-tracker@rtk_nmea_unified.service
# コードを更新し、テストを実行
sudo systemctl start gnss-tracker@rtk_nmea_unified.service
```

停止により送信待ちが消えることはありません。キューや状態ディレクトリを削除すると復元できないため、通常の停止・更新では削除しません。
