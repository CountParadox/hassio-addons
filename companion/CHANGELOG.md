# Changelog

## 5.1.3-1

- Added Home Assistant ingress: Companion can be opened inside Home Assistant,
  from the sidebar and through remote access, without exposing port 8000.

## 5.1.3

- Rebuilt the add-on on the official Bitfocus Companion image.
- Companion [v5.1.3](https://github.com/bitfocus/companion/releases/tag/v5.1.3).
- Data now lives in the add-on config folder. Data from the old add-on
  (`/config/companion`) is not migrated.
- Supported architectures are now `amd64` and `aarch64`.
