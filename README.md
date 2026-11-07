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
- `noodles.yaml` の形と、コードが読む環境変数の宣言漏れを確かめる。

`v1` のタグは、互換を壊さない変更のたびに付け直す。
