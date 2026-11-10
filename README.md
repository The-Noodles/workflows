# workflows

The Noodles Hub でデプロイするリポジトリが使う、公開のワークフロー。

各リポジトリに `.github/workflows/noodles.yml` を置く（ファイル名は固定。Hubはこのファイルの完了だけを見る）。

```yaml
name: noodles
on:
  push:
  pull_request:
permissions:
  contents: read
  id-token: write
jobs:
  build:
    uses: The-Noodles/workflows/.github/workflows/build.yml@v1
```

サブディレクトリに置くときは `with: { path: apps/web }`。

- `compose.yaml` の `build:` があるサービスを、Dockerfile があればそれで、なければ Railpack でビルドする。
- GitHub ActionsのOIDCトークンで `registry.noodles.jp` に push する。秘密情報は要らない。
- ビルドの層は GitHub Actions のキャッシュに置いて使い回す。コードだけを変えたときは、依存を入れ直さず、変わった層だけを push する。
- `noodles.yaml` の形と、コードが読む環境変数の宣言漏れを確かめる。

`v1` はブランチ。互換を壊さない変更は main に入れてから `v1` を進め、壊すときは `v2` を作る。


`.github/workflows/` か `scripts/` か `test/` を変えて push すると、`selftest.yml` が `test/` のサンプルを実際にビルドして push する（`v1` 以外のブランチ）。時間は Actions の実行結果で見る。
