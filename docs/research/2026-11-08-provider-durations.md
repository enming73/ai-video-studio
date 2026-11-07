# Video provider duration evidence

Checked 2026-11-08; these ranges apply to the current text-only payloads, not arbitrary media combinations. They are not proof of account availability or cost.

- MiniMax `MiniMax-H3`: integers 4–15 seconds, 768P supported. [MiniMax V2 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create).
- Alibaba `wan3.0-video`: integers 2–30 seconds without video input; different constraints apply with video input. [Alibaba Cloud API](https://www.alibabacloud.com/help/en/model-studio/wan3-video-generation-api-reference).
- fal `blackforestlabs/flux-3-max/text-to-video`: integers 5–20 seconds; the schema also specifies model-specific queue request paths. [fal OpenAPI schema](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=blackforestlabs%2Fflux-3-max%2Ftext-to-video).

Vendor limits and model availability can change; this is not live-provider testing.
