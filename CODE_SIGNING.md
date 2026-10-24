# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by
[SignPath Foundation](https://signpath.org/).

## What is signed

Only release builds of `ModUpdater.exe` published on the
[Releases](https://github.com/TheMrEvgen/MinecraftModUpdater/releases) page. Every release is built from this
repository by [GitHub Actions](.github/workflows/release.yml) from a tagged commit — never on a personal computer.
Third-party components are included unmodified and are not signed separately.

## Team roles

| Role | Members |
|---|---|
| Committers and reviewers | [TheMrEvgen](https://github.com/TheMrEvgen) |
| Approvers | [TheMrEvgen](https://github.com/TheMrEvgen) |

All changes go through the public repository history. Each signing request is approved manually by an approver.

## Privacy

This program will not transfer any information to other networked systems unless it is needed for the features the
user enabled (checking and downloading mod updates, checking for app updates). See the [privacy policy](PRIVACY.md)
for the full list of services and what is sent.
