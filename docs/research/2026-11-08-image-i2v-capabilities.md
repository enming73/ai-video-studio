# Image and I2V capability evidence

Checked 2026-11-08. These are documented interfaces, not proof of account access, current price, proxy compatibility or successful live calls.

- [OpenAI image generation guide](https://platform.openai.com/docs/guides/image-generation): `gpt-image-2.5-sunburst` supports text-to-image through `/images/generations` and image editing through `/images/edits`. The provider's price and the user's actual model access must be checked separately.
- [MiniMax H3 V2 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create): `MiniMax-H3` accepts `image_url` with role `first_frame`, including an image data URI; output duration is an integer 4–15 seconds.
- [fal Flux 3 Max image-to-video OpenAPI](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=blackforestlabs%2Fflux-3-max%2Fimage-to-video): the model accepts an `image_url` and integer duration 5–20 seconds. [fal file documentation](https://fal.ai/docs/documentation/model-apis/fal-cdn) says some, but not all, models accept data URIs; the model-specific schema does not prove data URI support. Do not enable an unverified inline payload solely on that basis.
