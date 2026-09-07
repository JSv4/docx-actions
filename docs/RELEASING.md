# Releasing DOCX Actions

The root action and `.github/workflows/review.yml` are released together from
the same tested commit. The reusable workflow checks out that exact revision
when it runs, including when the caller uses a tag.

1. Update the documentation and release notes, then wait for CI on the release
   commit to pass. CI runs the test suite, browser checks, and a real comparison
   through the installed composite action.
2. Create an annotated version tag such as `v2.0.1` at that commit. Version-specific
   tags stay fixed. Update the matching major alias (for example, `v2`) to the tested commit for compatible releases;
   breaking changes start a new major version.
3. Exercise the reusable workflow from the separate demo repository using the
   release tag. Check default comments without Pages permissions, latest/both modes, and the explicitly enabled Pages viewer. Verify an unrelated Pages site is refused.
4. Publish a GitHub release for the version-specific tag with installation
   instructions and validation results. A major-version alias is a Git tag, not a
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

Version 2 changes the default to comments without Pages. Leave `v1` and `v1.0.0`
unchanged when releasing v2; existing Pages users opt in with `pages: true`.
