# 記録・送信の実装詳細

## 測位データ

- 有効なチェックサムのRMC/GGAを同一GNSS時刻で結合し、UTCの `:00/:05/:10…` を選びます。受信機が該当時刻を出力しなければその枠は欠測になります。
- GGAの日付は新しいRMCの日付から前日・当日・翌日の最も近いものを選択。15秒以上ずれる組み合わせや古いRMCを拒否します。GGA先着は最大2秒保持。
- `measured_at` はGNSS UTC、`recorded_at` は機器が記録したUTC。Harvest受信時刻はクラウド側の時刻です。再送で測位時刻を書き換えません。
- 同一時刻の有効RMCとGGAが揃った座標だけ `position_valid=true` にします。NO_FIX・INCOMPLETEは日時付きで記録し、座標をnullにします。
- `receiver`（RTK/CLAS）、`run`、`event_id`、`fix_status`、衛星数、HDOP、標高、ジオイド高、補正経過秒、速度、針路、元のRMC/GGAを共通形式で記録。
- FIX/FLOATはGGAのqualityを優先します。`correction_configured` は構成名であり、その補強サービスの利用を実測確認したフラグではありません。
- QGISは既定の `measured_at,lat,lon,receiver,run,fix_status` で読み込めます。NO_FIX等は位置レイヤに含まれません。FIX率の分母にする場合は元の全レコードも使用してください。

## 保存・送信・再送

送信先の既定は `http://harvest.soracom.io`。Harvestだけに送信し、Unified Endpointの他サービスへの同時転送は行いません。送信先までの経路がセルラーであることを確認してください。NtripはOSの通常ルーティングに従います。

- HTTPの前にSQLiteへ保存。WAL＋synchronous=FULLを使用し、プロセス再起動後も未送信分を読み出します。SDカード自体の故障や電源断への完全な耐性を保証しません。
- キューは既定で `~/.local/state/gnss-comparison/{rtk,clas}/outbox.sqlite3`。50,000件、JSON本体合計100 MiBを上限とし、上限超過時は最古から破棄して件数を記録します。5秒周期で件数上限は約69時間。DB・索引・WALの物理サイズはJSON上限より大きくなります。
- 成功応答（HTTP 2xx）の確認後だけ送信待ちから削除。通信失敗・408/425/429・5xxは5/10/20/40/60秒＋小さなランダム待ちで再試行。Retry-Afterを尊重し、最大1時間待ちます。
- 401/403はデータを保持して60秒後に再試行。その他4xxやリダイレクトは隔離し、後続を止めません。隔離データは `queue.state='quarantine'` で確認し、原因修正後に対象レコードだけ手動再送を判断します。
- 再送でも本文とevent_idは不変。サーバー保存後に応答が失われた場合は重複し得ます。Harvest側の冪等性は仮定せず、分析側でevent_idを重複除外してください。以前のQGIS試作は同一受信機・実験・測位時刻・座標・状態の完全一致を除外します。
- キュー満杯・隔離・再送回数は `status.json` とjournalで確認。HTTPをバックグラウンドで行い、NMEA読み取りを待たせません。
- USB障害や30秒間のRMC/GGA途絶時は終了し、systemdが再起動。終了時も未送信データを保持します。
- Ntripは接続失敗後に再接続。30秒間補正データがなければ接続し直し、15秒以上古いGGAは送らず、HTTPヘッダーと同時に届いた補正データも捨てません。

## 検証・読み戻し

```sh
.venv/bin/python -m unittest discover -s tests -v
systemctl status qlm29h-nmea-unified.service   # または gnss-clas-harvest.service
journalctl -u qlm29h-nmea-unified.service -n 30 --no-pager
```

実データで両方の `run` と5秒刻みの `measured_at` を照合し、Harvestからevent_id・測位状態を読み戻します。通信失敗検証は送信先だけを一時的に到達不能にし、退避したevent_idがサービス再起動後も残り、復旧後にHarvestへ届くことを確認します。NtripやSSHの経路を丸ごと遮断しません。自動復元のタイマーを設け、検証後に必ず元の送信先とキュー残量を読み戻してください。

実機の個別設定と実測ログはこのリポジトリには含めません。Pi全体の再起動、物理電源断、長期SD耐久性、走行精度は別の検証です。
