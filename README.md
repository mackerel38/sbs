# sbs

Hypixel SkyBlock のプロフィールを GitHub Actions で取得し、分析に必要な項目だけを `data/stats.json` に保存します。

## 使い方

1. Settings → Secrets and variables → Actions で、`HYPIXEL_API_KEY` を登録する
2. Actions → Fetch SkyBlock stats → Run workflow を実行する

API キーは Secrets にだけ保存し、コードやチャットには書かないでください。
