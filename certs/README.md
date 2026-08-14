# Custom certificate authority

Place the public root certificate for an internal HTTPS Dawarich endpoint at
`certs/rootCA.pem`, or set `CUSTOM_CA_PATH` to its host path. Never place the
CA private key in this directory.

At container startup this certificate is appended to certifi's public trust
bundle, so both internal services and Strava remain trusted.
