# Releasing DOCX Actions

The root action and `.github/workflows/review.yml` are released together from
the same tested commit. The reusable workflow checks out that exact revision
when it runs, including when the caller uses a tag.

1. Update the documentation and release notes, then wait for CI on the release
   commit to pass. CI runs the test suite, browser checks, and a real comparison
   through the installed composite action.
2. Create an annotated version tag such as `v1.0.1` at that commit. Version-specific
   tags stay fixed. Update `v1` to the tested commit for compatible releases;
   breaking changes start a new major version.
3. Exercise the reusable workflow from the separate demo repository using the
   release tag. Check the deployed viewer and updating PR comment.
4. Publish a GitHub release for the version-specific tag with installation
   instructions and validation results. The `v1` alias is a Git tag, not a
   separate GitHub release.
5. In GitHub's release editor, select **Publish this Action to the GitHub
   Marketplace**, validate the metadata, and choose the categories. Suggested
   categories are **Code review** and **Utilities**. The repository owner must
   accept the Marketplace Developer Agreement if GitHub requests it.

The Marketplace listing is generated from the root action metadata and README.
Keep the reusable workflow installation prominent: it provides automatic PR
comments and Pages publishing in addition to comparison. The root action is
also available separately for workflows that only need comparison artifacts.

See GitHub's [Marketplace publishing instructions](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace)
and [release tag guidance](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/using-immutable-releases-and-tags-to-manage-your-actions-releases).
