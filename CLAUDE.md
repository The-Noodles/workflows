# workflows

The Noodles Hub でデプロイするリポジトリが呼ぶ、公開の再利用ワークフロー。仕様は The-Noodles/hub の `docs/platform.md`（「リポジトリに置くもの」「レジストリへのpush」）。

- 秘密情報を置かない。レジストリへは GitHub Actions のOIDCトークンで push する。
- イメージの名前の決まりは Hub の `modules/platform/server/compose.ts` の `imageName` と合わせる。
- ツールとアクションの版は固定する。Railpack の版を変えたら、`scripts/build.py` のフロントエンドの版もそろえる。
- 呼び出し側は `@v1` を使う。`v1` はブランチ（タグはこの環境からpushできない）。互換を壊さない変更は main に入れてから `v1` を進め、壊すときは `v2` を作る。
- コメントは保守のための最低限の説明だけ。変更履歴や変更理由は書かない。コミットメッセージとドキュメントは日本語。
