# Vendored frontend libraries

PingWatch ships its third-party JS dependencies locally so the kiosk works
without an outbound network.

## Versions

| File                  | Library    | Version | Source                                                |
|-----------------------|------------|---------|-------------------------------------------------------|
| `alpine.min.js`       | Alpine.js  | 3.14.x  | https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js |
| `chart.umd.min.js`    | Chart.js   | 4.4.x   | https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js |

## Re-fetch

```sh
curl -sSL -o alpine.min.js     https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js
curl -sSL -o chart.umd.min.js  https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js
```

In CI/release: verify SHA256 against a known-good fingerprint before checking
the files into the repo.
