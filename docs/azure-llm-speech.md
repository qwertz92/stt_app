# Azure LLM Speech (MAI-Transcribe) Setup

Azure LLM Speech is a **remote, cloud-only** transcription engine. Its enhanced
mode is backed by Microsoft's **MAI-Transcribe** models
(`mai-transcribe-1.5`, `mai-transcribe-1`) from the Microsoft AI (MAI) team,
served through Microsoft Foundry / the Azure Speech service.

There is **no local / ONNX runtime** for these models, and Microsoft does not
publish their parameter count. The feature is currently in **public preview**
(no SLA).

In this app, Azure runs in **batch mode only** (the synchronous
"fast transcription" `:transcribe` API). It is not wired into streaming mode.

## Why you need both an endpoint and a key

Every other remote provider in this app needs only an API key. Azure is the
exception: the request URL is **per-resource**, so you must supply **two**
things:

1. **Endpoint** — your resource URL, e.g.
   `https://<resource-name>.cognitiveservices.azure.com`
2. **Key** — the resource access key

Both come from the same place in the Azure portal (see below).

## Prerequisites

- An Azure subscription ([free account](https://azure.microsoft.com/free/)).
- An **Azure AI Speech / Foundry** resource in a **region that supports LLM
  Speech**. If enhanced mode returns `Enhanced mode is currently not supported
  yet`, the resource is in an unsupported region — create one in a supported
  region instead.

## Step-by-step setup

1. **Create the resource**
   - In the [Azure portal](https://portal.azure.com), create a
     *Azure AI Services* / *Speech* resource in a region that supports LLM
     Speech.

2. **Copy the endpoint and key**
   - Open the resource → **Keys and Endpoint**.
   - Copy the **Endpoint** (looks like
     `https://<resource>.cognitiveservices.azure.com`).
   - Copy **KEY 1** (or KEY 2).

3. **Configure the app**
   - Open **Settings → Remote Provider API Keys**.
   - Paste the key into the **Azure** field.
   - Paste the endpoint into the **Azure Endpoint** field (directly below the
     provider keys).
   - Click **Save API Keys** (the endpoint is saved together with the keys).

4. **Verify**
   - Set **Connection Target** to *Azure only* and click **Run Connection
     Test**. The app posts a ~1-second silent clip to validate the endpoint,
     key, and region. A green result means you are ready.

5. **Use it**
   - On the General tab, set **Engine** to *Remote (Azure LLM Speech)*.
   - Pick a **Remote Model** (`mai-transcribe-1.5` is the default and covers the
     most languages).
   - Dictate as usual — Azure transcribes after you stop (batch).

> The app accepts a full endpoint URL, a bare host
> (`<resource>.cognitiveservices.azure.com`), or just the resource name; it
> normalizes all three to the correct `:transcribe` URL.

## Models

| Model | Notes |
|-------|-------|
| `mai-transcribe-1.5` (default) | 42 languages, phrase-list/entity biasing, verbatim/readability styles |
| `mai-transcribe-1` | First generation, smaller language set |

## Languages

Multilingual by default (`Auto`). Selecting a specific language sends a `locales`
hint to the service. German and English are both supported. The app maps its
`no` (Norwegian) code to Azure's `nb` locale automatically.

## Cost and free tier

- **Free (F0) tier:** 5 audio hours/month for speech to text (hard monthly cap,
  not adjustable).
- **Pay-as-you-go (Standard, S0):** ~$0.36/hour for fast transcription.
- Always confirm current pricing on the
  [Azure Speech pricing page](https://azure.microsoft.com/pricing/details/speech/).

## Limits and notes

- **Batch only** in this app (no streaming/live partials).
- **Public preview** — behavior and pricing can change; no SLA.
- Audio: common formats (WAV, MP3, FLAC, OGG/OPUS, etc.); MAI-Transcribe accepts
  files up to ~300 MB.
- Data leaves your machine and is processed in Azure. For fully offline use,
  pick a local engine instead.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Authentication failed (HTTP 401/403)` | Key is wrong, or the key does not match the configured endpoint/region. Re-copy both from **Keys and Endpoint**. |
| `Endpoint not found (HTTP 404)` | Endpoint URL is wrong, or the resource is in a region without LLM Speech. |
| `Bad request (HTTP 400) … Enhanced mode … not supported` | The region does not support LLM Speech yet. Create the resource in a supported region. |
| `SSL certificate verification failed` | Corporate proxy (e.g. Zscaler). See [SSL / proxy issues](advanced-setup.md#ssl--proxy-issues). |

## References

- LLM Speech API: <https://learn.microsoft.com/azure/ai-services/speech-service/llm-speech>
- MAI-Transcribe model: <https://learn.microsoft.com/azure/ai-services/speech-service/mai-transcribe>
- Azure Speech pricing: <https://azure.microsoft.com/pricing/details/speech/>
- Cost comparison across providers: [provider-costs.md](provider-costs.md)
