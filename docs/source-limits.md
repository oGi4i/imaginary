# Raster source admission

Source validation inspects container bytes before the image operation. It accepts
single-frame JPEG, PNG and WebP and rejects PDF, SVG, TIFF, HEIF/HEIC, GIF,
animation, malformed containers and unrecognized input. It does not decode the
compressed image or check PNG CRCs; native processing can still report a decode
error for an admitted container.

`-max-source-pixels` sets the maximum source width multiplied by height. The
16,000,000-pixel default is provisional and is not qualified for deployment.
Nonpositive values use that default. Validation returns JSON with HTTP 400 for
malformed or unrecognized data, 406 for excluded formats or animation, and 413
for a source over the pixel limit. These responses bypass the native placeholder.

Image watermarks are disabled for this candidate, including inside pipelines;
the service returns HTTP 406 without fetching the secondary source. Pipelines
check every operation for image watermarks before other pipeline validation.
Other pipeline validation failures return fixed JSON HTTP 400 without a native
placeholder or reflected parameter values.

This is not a complete native-memory bound. Output enlargement and intermediate
pipeline images are not covered by the source-pixel check. Their work budget and
the candidate require qualification before deployment.
