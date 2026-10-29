# 公開用コピーの検証記録

確認日: 2026-10-30 JST

- 環境: macOS / Python 3.12。専用venvへ`requirements.txt`をインストール。
- `python -m unittest discover -s tests -v`: **18件成功**。
- RTK・CLAS両起動スクリプトの`--help`: 正常終了。
- 共通モジュール・起動スクリプト・既存テストは、元の車載実験用ソースとSHA-256が一致。
- 認証値、環境ファイル、SIM情報、SQLiteキュー、実測ログを配布対象に含めていないことを確認。

今回の公開作業では、ラズパイへの再配置、受信機への設定送信、NTRIPの実接続、Harvestへの送信、systemdテンプレートの実機導入は行っていません。元の実機試験と、この公開用コピーの単体テストは別の検証です。

依存パッケージ:

```text
certifi==2026.10.23
charset-normalizer==3.5.2
idna==3.20
pynmea2==1.19.0
pyserial==3.5
requests==2.34.2
urllib3==2.8.0
```

`requirements.lock`は元の実機配置時の記録で、今回のvenvではcertifiのみ異なる版になっています。
