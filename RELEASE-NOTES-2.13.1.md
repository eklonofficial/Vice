# Vice 2.13.1

## Fixes

- Existing clip links update when the public tunnel becomes available. Tunnel
  startup retries transient failures and reports useful error details.
- GSR can retry with software encoding when the available hardware encoder
  cannot start.
- Mix-first audio no longer asks GSR to combine application and inverse
  application sources in one unsupported track.
- MP4 files with zero duration metadata remain readable when their frame count
  and frame rate provide a reliable duration.
- Scrolling inside a clip context menu no longer dismisses it.

Thanks to KITE-Force for the context menu fix.
