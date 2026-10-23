# Third-party code

The subscription conversion core in `app/vendor/cf-sub/core.mjs`, client SVG icons,
and aspects of the page layout/palette are adapted from the local project
`cf-ips-to-sub-link` (CloudflareSub), copyright (c) 2026, under the MIT License.
The original license is included in `app/vendor/cf-sub/LICENSE`.

Local changes include validation, unique endpoint names, preserving address remarks,
Trojan SNI output, robust YAML quoting, server-side caching, and local SQLite storage.
The original Workers runtime, KV bindings, remote subscription fetching, and
browser plaintext-password storage are not used.

The installer downloads official Xray-core and Node.js releases, verifies release
checksums, and installs Certbot and qrcode from PyPI. These dependencies retain
their respective upstream licenses. They are not bundled in this repository.
