# Windows release signing

## SignPath setup

1. [Apply for SignPath Foundation code signing](https://signpath.org/apply) with the public repository URL `https://github.com/Zoltak-Dev/ai-multi-instance`. Enable MFA on both GitHub and SignPath. Review the [Code signing policy](CODE_SIGNING.md) and link it from the public README when you publish the setup. The workflow adds the same link to the signed release notes. [Foundation terms](https://signpath.org/terms)
2. After acceptance, add the predefined **GitHub.com** trusted build system to your SignPath organization. Create a project named **AI Multi-Instance** with slug `ai-multi-instance` and the repository URL above. Link **GitHub.com** to that project. [SignPath GitHub setup](https://docs.signpath.io/trusted-build-systems/github)
3. In **Users and Groups**, create a CI user named `github-actions` and save its API token. It only needs the project-level submitter permission in the next step. [SignPath CI users](https://docs.signpath.io/users/#ci-users)
4. In the project's **Artifact Configurations**, create `windows-zip` and paste the contents of [windows-zip.xml](windows-zip.xml) into the XML editor. Both executables are signed inside the ZIP and checked against the same version.
5. In **Signing Policies**, create `release-signing` with purpose **Release signing** and the **SignPath Foundation** certificate. Set `github-actions` as the submitter. Enable **Trusted build system verification** and **Verify origin**, with `main` as the allowed branch. Enable **Use approval process**, select your own SignPath account as an approver, and require one approval. [Signing policy settings](https://docs.signpath.io/projects/#signing-policies)
6. On GitHub, open **Settings → Secrets and variables → Actions**. In **Secrets**, add `SIGNPATH_API_TOKEN` with the CI user's token. In **Variables**, add `SIGNPATH_ORGANIZATION_ID` with the UUID from the SignPath organization page. [GitHub Actions settings](https://github.com/Zoltak-Dev/ai-multi-instance/settings/secrets/actions)

## Publish a Windows release

1. Increase `MANAGER_VERSION` in `engine.py`. In the same commit, move the entries being released from `## Unreleased` in [CHANGELOG.md](../../CHANGELOG.md) into a new `## vX.Y.Z — Short title` section. Keep `## Unreleased` for future changes.
2. Write the section for people downloading the update. Use a few bullets about what changed and `###` for optional subheadings. The full section heading becomes the GitHub release title. Its body becomes the release description, followed by download and signing links. The job reports an error before building if the matching section is missing, empty, or duplicated.
3. Push the bump and changelog commit to `main`. A push with the same version runs the usual checks. A version increase builds the Windows ZIP and submits it to SignPath after the tests pass.

For a source-only release, put `<!-- source-only -->` on the first line of the section body. The workflow then waits for tests and publishes the release with GitHub's source archives. Later releases use the signing workflow by default.

Open the signing request from the GitHub Actions log or SignPath notification, verify the commit and version, and approve it within one hour. GitHub Actions verifies both timestamped signatures, then publishes the `vX.Y.Z` release and its `ai-multi-instance-vX.Y.Z-windows.zip` asset under [Releases](https://github.com/Zoltak-Dev/ai-multi-instance/releases).

If the signing job times out, use **Re-run failed jobs** on that run and approve the new request. If publication stops partway, rerunning the failed job resumes its draft. The workflow leaves published releases intact.
