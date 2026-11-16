# Code signing policy

Windows releases signed through GitHub Actions use free code signing provided by [SignPath.io](https://about.signpath.io/), with a certificate from [SignPath Foundation](https://signpath.org/).

The repository maintainer, [Zoltak-Dev](https://github.com/Zoltak-Dev), owns commits and reviews contributions. The maintainer approves each SignPath request after checking its source commit and release version.

## Privacy

Profile and session data is stored locally. To check quotas, the manager uses the relevant saved session with its own provider, [OpenAI](https://openai.com/policies/privacy-policy/) or [Anthropic](https://www.anthropic.com/legal/privacy). The manager contacts [GitHub](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) to check for updates. GitHub Actions sends only the release build to SignPath for signing.
