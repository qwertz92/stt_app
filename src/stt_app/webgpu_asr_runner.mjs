import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

let AutoProcessor;
let GraniteSpeechForConditionalGeneration;
let pipeline;
let Tokenizer;
let ort;

async function loadRuntimeDependencies() {
  const transformers = await import("@huggingface/transformers");
  const tokenizers = await import("@huggingface/tokenizers");
  AutoProcessor = transformers.AutoProcessor;
  GraniteSpeechForConditionalGeneration =
    transformers.GraniteSpeechForConditionalGeneration;
  pipeline = transformers.pipeline;
  Tokenizer = tokenizers.Tokenizer;
  ort = await import("onnxruntime-node");
  transformers.env.allowLocalModels = true;
  transformers.env.allowRemoteModels = false;
  transformers.env.useBrowserCache = false;
  transformers.env.useFSCache = true;
}

const TARGET_SAMPLE_RATE = 16000;
const GRANITE_MAX_CHUNK_SECONDS = 30;
const GRANITE_BOUNDARY_CONTEXT_SECONDS = 5;
const GRANITE_MIN_ENERGY_WINDOW_SAMPLES = 1600;
const GRANITE_4_1_AUDIO_TOKEN_ID = 100352;
const GRANITE_4_1_EOS_TOKEN_ID = 100257;
const GRANITE_4_1_HIDDEN_SIZE = 2048;
const GRANITE_4_1_NUM_LAYERS = 40;
const GRANITE_4_1_NAR_EMBEDDING_MULTIPLIER = 12;
// The NAR encoder's BPE/CTC head emits vocab+1 classes: a blank PREPENDED at
// index 0, then the LLM token ids shifted up by one. CTC decode collapses
// repeats, drops the blank (0), and shifts class c -> LLM token (c - 1).
const GRANITE_4_1_NAR_CTC_BLANK_ID = 0;
const GRANITE_4_1_NAR_CTC_TOKEN_OFFSET = 1;
const ORT_NEG_INF = -3.4028234663852886e38;
const MAX_WAV_DATA_BYTES = 512 * 1024 * 1024;
const MAX_WAV_FRAMES = 16000 * 60 * 60 * 8;
const MAX_PROTOCOL_LINE_CHARS = 1024 * 1024;

// Models that load through the high-level Transformers.js
// GraniteSpeechForConditionalGeneration pipeline (q4 packages). Granite 4.1 2B
// shares Granite 4.0's component layout, so it uses the same path.
const GRANITE_PIPELINE_MODELS = new Set([
  "granite-4.0-1b-speech",
  "granite-speech-4.1-2b",
]);

// Granite 4.1 variants that still run through explicit onnxruntime-node graph
// sessions because no Transformers.js q4 package exists for them yet.
const GRANITE_4_1_MODEL_LAYOUTS = new Map([
  ["granite-speech-4.1-2b-plus", "granite_4_1_ar"],
  ["granite-speech-4.1-2b-nar", "granite_4_1_nar"],
]);

function parseArgs(argv) {
  const args = {
    server: false,
    model: "",
    modelPath: "",
    device: "auto",
    dtype: "q4",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--server") {
      args.server = true;
      continue;
    }
    if (value === "--model") {
      args.model = argv[index + 1] || "";
      index += 1;
      continue;
    }
    if (value === "--model-path") {
      args.modelPath = argv[index + 1] || "";
      index += 1;
      continue;
    }
    if (value === "--device") {
      args.device = argv[index + 1] || "auto";
      index += 1;
      continue;
    }
    if (value === "--dtype") {
      args.dtype = argv[index + 1] || "q4";
      index += 1;
    }
  }
  return args;
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function formatError(error) {
  if (error && error.stack) {
    return String(error.stack);
  }
  if (error && error.message) {
    return String(error.message);
  }
  return String(error);
}

function conciseError(error) {
  const firstLine = formatError(error)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
  return String(firstLine || error || "unknown error").slice(0, 600);
}

export function parseProtocolRequestLine(rawLine) {
  if (rawLine.length > MAX_PROTOCOL_LINE_CHARS) {
    throw new Error("Protocol request line is too large.");
  }
  const line = rawLine.trim();
  if (!line) {
    return null;
  }
  let request;
  try {
    request = JSON.parse(line);
  } catch (error) {
    throw new Error(`Invalid JSON request: ${formatError(error)}`);
  }
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("Protocol request must be a JSON object.");
  }
  return request;
}

function modelPathForTransformers(modelPath) {
  return String(modelPath || "").replaceAll("\\", "/");
}

function readAscii(buffer, offset, length) {
  return buffer.toString("ascii", offset, offset + length);
}

export function findChunk(buffer, chunkId) {
  let offset = 12;
  while (offset + 8 <= buffer.length) {
    const id = readAscii(buffer, offset, 4);
    const size = buffer.readUInt32LE(offset + 4);
    const dataOffset = offset + 8;
    const dataEnd = dataOffset + size;
    if (dataEnd > buffer.length) {
      throw new Error(
        `Invalid WAV file: ${id || "unknown"} chunk exceeds the file bounds.`,
      );
    }
    if (id === chunkId) {
      return { offset: dataOffset, size };
    }
    offset = dataEnd + (size % 2);
    if (offset > buffer.length && dataEnd !== buffer.length) {
      throw new Error(`Invalid WAV file: ${id || "unknown"} chunk padding is truncated.`);
    }
  }
  return null;
}

function decodePcmSample(buffer, byteOffset, bitsPerSample) {
  if (bitsPerSample === 8) {
    return (buffer.readUInt8(byteOffset) - 128) / 128;
  }
  if (bitsPerSample === 16) {
    return Math.max(-1, buffer.readInt16LE(byteOffset) / 32768);
  }
  if (bitsPerSample === 24) {
    const raw = buffer.readUIntLE(byteOffset, 3);
    const signed = raw & 0x800000 ? raw | 0xff000000 : raw;
    return Math.max(-1, signed / 8388608);
  }
  if (bitsPerSample === 32) {
    return Math.max(-1, buffer.readInt32LE(byteOffset) / 2147483648);
  }
  throw new Error(`Unsupported PCM WAV bit depth: ${bitsPerSample}`);
}

function decodeFloatSample(buffer, byteOffset, bitsPerSample) {
  if (bitsPerSample === 32) {
    return buffer.readFloatLE(byteOffset);
  }
  if (bitsPerSample === 64) {
    return buffer.readDoubleLE(byteOffset);
  }
  throw new Error(`Unsupported float WAV bit depth: ${bitsPerSample}`);
}

function resampleLinear(audio, sourceRate, targetRate) {
  if (sourceRate === targetRate) {
    return audio;
  }
  if (sourceRate <= 0 || targetRate <= 0) {
    throw new Error(`Invalid WAV sample rate: ${sourceRate}`);
  }
  const targetLength = Math.max(1, Math.round(audio.length * targetRate / sourceRate));
  const output = new Float32Array(targetLength);
  const ratio = sourceRate / targetRate;
  for (let index = 0; index < targetLength; index += 1) {
    const position = index * ratio;
    const leftIndex = Math.floor(position);
    const rightIndex = Math.min(leftIndex + 1, audio.length - 1);
    const fraction = position - leftIndex;
    output[index] = audio[leftIndex] * (1 - fraction) + audio[rightIndex] * fraction;
  }
  return output;
}

export function decodeWavFile(audioPath, targetSampleRate) {
  const buffer = readFileSync(audioPath);
  if (
    buffer.length < 44 ||
    readAscii(buffer, 0, 4) !== "RIFF" ||
    readAscii(buffer, 8, 4) !== "WAVE"
  ) {
    throw new Error(
      "The ONNX runtime can decode WAV input only. Use a WAV benchmark sample or the app's last recording.",
    );
  }
  const riffEnd = buffer.readUInt32LE(4) + 8;
  if (riffEnd < 12 || riffEnd > buffer.length) {
    throw new Error("Invalid WAV file: RIFF size exceeds the file bounds.");
  }

  const fmt = findChunk(buffer, "fmt ");
  const data = findChunk(buffer, "data");
  if (!fmt || !data) {
    throw new Error("Invalid WAV file: missing fmt or data chunk.");
  }
  if (fmt.offset + fmt.size > riffEnd || data.offset + data.size > riffEnd) {
    throw new Error("Invalid WAV file: chunk exceeds the declared RIFF size.");
  }
  if (fmt.size < 16) {
    throw new Error("Invalid WAV file: fmt chunk is too small.");
  }

  const audioFormat = buffer.readUInt16LE(fmt.offset);
  const channelCount = buffer.readUInt16LE(fmt.offset + 2);
  const sampleRate = buffer.readUInt32LE(fmt.offset + 4);
  const blockAlign = buffer.readUInt16LE(fmt.offset + 12);
  const bitsPerSample = buffer.readUInt16LE(fmt.offset + 14);
  if (channelCount <= 0 || blockAlign <= 0) {
    throw new Error("Invalid WAV file: channel count or block alignment is zero.");
  }
  if (data.size > MAX_WAV_DATA_BYTES) {
    throw new Error("WAV input is too large for the local ONNX runtime.");
  }
  if (!Number.isInteger(blockAlign / channelCount)) {
    throw new Error("Invalid WAV file: block alignment does not match channel count.");
  }
  const bytesPerSample = Math.ceil(bitsPerSample / 8);
  const bytesPerChannel = blockAlign / channelCount;
  if (bytesPerSample <= 0 || bytesPerChannel < bytesPerSample) {
    throw new Error("Invalid WAV file: sample width exceeds block alignment.");
  }
  const frameCount = Math.floor(data.size / blockAlign);
  if (data.size % blockAlign !== 0) {
    throw new Error("Invalid WAV file: data chunk ends with a partial audio frame.");
  }
  const isPcm = audioFormat === 1 || audioFormat === 65534;
  const isFloat = audioFormat === 3;
  if (!isPcm && !isFloat) {
    throw new Error(`Unsupported WAV encoding: ${audioFormat}. Use PCM or float WAV.`);
  }
  const supportedBits = isFloat ? [32, 64] : [8, 16, 24, 32];
  if (!supportedBits.includes(bitsPerSample)) {
    throw new Error(`Unsupported WAV bit depth: ${bitsPerSample}.`);
  }
  if (frameCount <= 0) {
    throw new Error("Invalid WAV file: data chunk contains no complete audio frame.");
  }
  if (frameCount > MAX_WAV_FRAMES) {
    throw new Error("WAV input contains too many audio frames.");
  }
  const mono = new Float32Array(frameCount);

  for (let frame = 0; frame < frameCount; frame += 1) {
    const frameOffset = data.offset + frame * blockAlign;
    let sum = 0;
    for (let channel = 0; channel < channelCount; channel += 1) {
      const sampleOffset = frameOffset + channel * bytesPerChannel;
      const sample = isFloat
        ? decodeFloatSample(buffer, sampleOffset, bitsPerSample)
        : decodePcmSample(buffer, sampleOffset, bitsPerSample);
      sum += Number.isFinite(sample) ? sample : 0;
    }
    mono[frame] = Math.max(-1, Math.min(1, sum / channelCount));
  }

  return resampleLinear(mono, sampleRate, targetSampleRate);
}

function findQuietestSplitPoint(audio, start, end, windowSamples) {
  let bestIndex = start;
  let bestEnergy = Infinity;
  const step = Math.max(1, Math.floor(windowSamples / 2));
  for (let index = start; index < end; index += step) {
    const windowEnd = Math.min(index + windowSamples, end);
    if (windowEnd <= index) {
      break;
    }
    let energy = 0;
    for (let sampleIndex = index; sampleIndex < windowEnd; sampleIndex += 1) {
      const sample = audio[sampleIndex] || 0;
      energy += sample * sample;
    }
    energy /= windowEnd - index;
    if (energy < bestEnergy) {
      bestEnergy = energy;
      bestIndex = index + Math.floor((windowEnd - index) / 2);
    }
  }
  return bestIndex;
}

function splitAudioAtQuietBoundaries(audio, sampleRate, maxChunkSeconds) {
  const maxSamples = Math.max(1, Math.round(maxChunkSeconds * sampleRate));
  if (audio.length <= maxSamples) {
    return [audio];
  }

  const boundaryContextSamples = Math.max(
    1,
    Math.round(GRANITE_BOUNDARY_CONTEXT_SECONDS * sampleRate),
  );
  const chunks = [];
  let offset = 0;
  while (offset < audio.length) {
    const hardEnd = Math.min(offset + maxSamples, audio.length);
    if (hardEnd >= audio.length) {
      chunks.push(audio.slice(offset, audio.length));
      break;
    }

    const searchStart = Math.max(offset + 1, hardEnd - boundaryContextSamples);
    const splitPoint = findQuietestSplitPoint(
      audio,
      searchStart,
      hardEnd,
      GRANITE_MIN_ENERGY_WINDOW_SAMPLES,
    );
    const safeSplitPoint = Math.max(offset + 1, Math.min(splitPoint, audio.length));
    chunks.push(audio.slice(offset, safeSplitPoint));
    offset = safeSplitPoint;
  }
  return chunks;
}

function readJson(relativeRoot, relativePath) {
  return JSON.parse(readFileSync(join(relativeRoot, relativePath), "utf8"));
}

function int64Tensor(data, dims) {
  return new ort.Tensor(
    "int64",
    BigInt64Array.from(data, (value) => BigInt(value)),
    dims,
  );
}

function float32Tensor(data, dims) {
  return new ort.Tensor("float32", data, dims);
}

function argmax(data, offset, length) {
  let bestIndex = 0;
  let bestValue = -Infinity;
  for (let index = 0; index < length; index += 1) {
    const value = data[offset + index];
    if (value > bestValue) {
      bestValue = value;
      bestIndex = index;
    }
  }
  return bestIndex;
}

function uniqueConsecutiveWithout(ids, filteredId) {
  const result = [];
  let previous = null;
  for (const id of ids) {
    if (id === previous) {
      continue;
    }
    previous = id;
    if (id !== filteredId) {
      result.push(id);
    }
  }
  return result;
}

function hertzToMel(freq) {
  return 2595.0 * Math.log10(1.0 + freq / 700.0);
}

function melToHertz(mel) {
  return 700.0 * (10.0 ** (mel / 2595.0) - 1.0);
}

function linspace(start, end, count) {
  const output = new Float64Array(count);
  if (count === 1) {
    output[0] = start;
    return output;
  }
  const step = (end - start) / (count - 1);
  for (let index = 0; index < count; index += 1) {
    output[index] = start + index * step;
  }
  return output;
}

function buildMelFilterBank(nFft, nMels, sampleRate) {
  const frequencyBins = Math.floor(nFft / 2) + 1;
  const fftFreqs = linspace(0, Math.floor(sampleRate / 2), frequencyBins);
  const melFreqs = linspace(
    hertzToMel(0),
    hertzToMel(sampleRate / 2),
    nMels + 2,
  );
  const filterFreqs = Float64Array.from(melFreqs, melToHertz);
  const filters = Array.from(
    { length: nMels },
    () => new Float32Array(frequencyBins),
  );

  for (let mel = 0; mel < nMels; mel += 1) {
    const left = filterFreqs[mel];
    const center = filterFreqs[mel + 1];
    const right = filterFreqs[mel + 2];
    for (let bin = 0; bin < frequencyBins; bin += 1) {
      const freq = fftFreqs[bin];
      const down = (freq - left) / (center - left);
      const up = (right - freq) / (right - center);
      filters[mel][bin] = Math.max(0, Math.min(down, up));
    }
  }
  return filters;
}

function buildGraniteWindow(nFft, winLength) {
  const window = new Float64Array(nFft);
  const offset = Math.floor((nFft - winLength) / 2);
  for (let index = 0; index < winLength; index += 1) {
    // Periodic Hann, matching torch/Transformers.js window_function().
    window[offset + index] = 0.5 - 0.5 * Math.cos((2 * Math.PI * index) / winLength);
  }
  return window;
}

function reflectOffset(index, width) {
  return Math.abs(((index + width) % (2 * width)) - width);
}

function reflectPad(audio, left, right) {
  if (audio.length < 2) {
    const padded = new Float32Array(audio.length + left + right);
    padded.set(audio, left);
    return padded;
  }
  const padded = new Float32Array(audio.length + left + right);
  const width = audio.length - 1;
  padded.set(audio, left);
  for (let index = 1; index <= left; index += 1) {
    padded[left - index] = audio[reflectOffset(index, width)];
  }
  for (let index = 1; index <= right; index += 1) {
    padded[width + left + index] = audio[reflectOffset(width - index, width)];
  }
  return padded;
}

class Radix2Fft {
  constructor(size) {
    this.size = size;
  }

  transform(real, imag) {
    const n = this.size;
    let reverse = 0;
    for (let index = 1; index < n; index += 1) {
      let bit = n >> 1;
      while (reverse & bit) {
        reverse ^= bit;
        bit >>= 1;
      }
      reverse ^= bit;
      if (index < reverse) {
        const realValue = real[index];
        real[index] = real[reverse];
        real[reverse] = realValue;
        const imagValue = imag[index];
        imag[index] = imag[reverse];
        imag[reverse] = imagValue;
      }
    }

    for (let length = 2; length <= n; length <<= 1) {
      const angle = (-2 * Math.PI) / length;
      const stepReal = Math.cos(angle);
      const stepImag = Math.sin(angle);
      const half = length >> 1;
      for (let start = 0; start < n; start += length) {
        let wr = 1;
        let wi = 0;
        for (let offset = 0; offset < half; offset += 1) {
          const even = start + offset;
          const odd = even + half;
          const tr = wr * real[odd] - wi * imag[odd];
          const ti = wr * imag[odd] + wi * real[odd];
          real[odd] = real[even] - tr;
          imag[odd] = imag[even] - ti;
          real[even] += tr;
          imag[even] += ti;
          const nextWr = wr * stepReal - wi * stepImag;
          wi = wr * stepImag + wi * stepReal;
          wr = nextWr;
        }
      }
    }
  }
}

class Granite41AudioFrontend {
  constructor(config) {
    const melspec = config?.melspec_kwargs || config || {};
    this.nFft = Number(melspec.n_fft || 512);
    this.hopLength = Number(melspec.hop_length || 160);
    this.winLength = Number(melspec.win_length || 400);
    this.nMels = Number(melspec.n_mels || 80);
    this.sampleRate = Number(melspec.sample_rate || config?.sampling_rate || 16000);
    this.window = buildGraniteWindow(this.nFft, this.winLength);
    this.melFilters = buildMelFilterBank(this.nFft, this.nMels, this.sampleRate);
    this.fft = new Radix2Fft(this.nFft);
    this.real = new Float64Array(this.nFft);
    this.imag = new Float64Array(this.nFft);
    this.power = new Float64Array(Math.floor(this.nFft / 2) + 1);
  }

  extract(audio) {
    const rawFrames = Math.max(0, 1 + Math.floor((audio.length - 1) / this.hopLength));
    const maxFrames = rawFrames - (rawFrames % 2);
    if (maxFrames < 2) {
      throw new Error("Audio is too short for Granite 4.1 preprocessing.");
    }

    const padded = reflectPad(
      audio,
      Math.floor(this.nFft / 2),
      Math.floor(this.nFft / 2),
    );
    const availableFrames = Math.max(
      0,
      1 + Math.floor((padded.length - this.nFft) / this.hopLength),
    );
    const frameCount = Math.min(maxFrames, availableFrames);
    const evenFrameCount = frameCount - (frameCount % 2);
    if (evenFrameCount < 2) {
      throw new Error("Audio is too short for Granite 4.1 preprocessing.");
    }

    const mel = new Float32Array(evenFrameCount * this.nMels);
    let maxLogMel = -Infinity;

    for (let frame = 0; frame < evenFrameCount; frame += 1) {
      const audioOffset = frame * this.hopLength;
      this.real.fill(0);
      this.imag.fill(0);
      for (let index = 0; index < this.nFft; index += 1) {
        this.real[index] = (padded[audioOffset + index] || 0) * this.window[index];
      }
      this.fft.transform(this.real, this.imag);

      for (let bin = 0; bin < this.power.length; bin += 1) {
        this.power[bin] = this.real[bin] ** 2 + this.imag[bin] ** 2;
      }

      const melOffset = frame * this.nMels;
      for (let melIndex = 0; melIndex < this.nMels; melIndex += 1) {
        const filter = this.melFilters[melIndex];
        let value = 0;
        for (let bin = 0; bin < this.power.length; bin += 1) {
          value += filter[bin] * this.power[bin];
        }
        const logValue = Math.log10(Math.max(1e-10, value));
        mel[melOffset + melIndex] = logValue;
        if (logValue > maxLogMel) {
          maxLogMel = logValue;
        }
      }
    }

    const logThreshold = maxLogMel - 8.0;
    for (let index = 0; index < mel.length; index += 1) {
      mel[index] = (Math.max(mel[index], logThreshold) + 4.0) / 4.0;
    }

    const featureFrames = evenFrameCount / 2;
    const features = new Float32Array(featureFrames * this.nMels * 2);
    for (let frame = 0; frame < featureFrames; frame += 1) {
      const first = (2 * frame) * this.nMels;
      const second = first + this.nMels;
      const output = frame * this.nMels * 2;
      features.set(mel.subarray(first, first + this.nMels), output);
      features.set(mel.subarray(second, second + this.nMels), output + this.nMels);
    }

    return {
      inputFeatures: features,
      featureFrames,
      attentionMask: BigInt64Array.from(
        { length: featureFrames },
        () => 1n,
      ),
    };
  }
}

async function hasWebGpuAdapter() {
  const gpu = globalThis.navigator?.gpu;
  if (!gpu) {
    return false;
  }
  try {
    const adapter = await gpu.requestAdapter();
    return Boolean(adapter);
  } catch {
    return false;
  }
}

function resolveDevice(requestedDevice) {
  const requested = String(requestedDevice || "auto").toLowerCase();
  if (requested === "wasm") {
    throw new Error(
      "The Transformers.js Node runtime does not support device \"wasm\". Use \"cpu\" for CPU inference.",
    );
  }

  const gpuDevices = ["webgpu"];
  if (process.platform === "win32") {
    gpuDevices.push("dml");
  }

  if (requested === "gpu") {
    return gpuDevices;
  }

  if (["webgpu", "dml", "cpu"].includes(requested)) {
    return [requested];
  }

  if (requested !== "auto") {
    throw new Error(
      `Unsupported device policy: "${requestedDevice}". Use auto, gpu, webgpu, dml, or cpu.`,
    );
  }

  const devices = [];
  devices.push(...gpuDevices);
  devices.push("cpu");
  return devices;
}

function graniteDtype(dtype) {
  return {
    embed_tokens: dtype,
    audio_encoder: dtype,
    decoder_model_merged: dtype,
  };
}

const GRANITE_LANGUAGE_NAMES = {
  de: "German",
  en: "English",
  es: "Spanish",
  fr: "French",
  ja: "Japanese",
  pt: "Portuguese",
};

function granitePrompt(language) {
  const languageName = GRANITE_LANGUAGE_NAMES[language];
  if (languageName) {
    return `<|audio|>transcribe the ${languageName} speech into a written format.`;
  }
  return "<|audio|>can you transcribe the speech into a written format?";
}

function joinTranscriptChunks(texts) {
  return texts
    .map((text) => String(text || "").trim())
    .filter(Boolean)
    .join(" ")
    .replace(/\s+([,.;:!?])/g, "$1");
}

function granite41Prompt(language) {
  const languageName = GRANITE_LANGUAGE_NAMES[language];
  if (languageName) {
    return `<|audio|>transcribe the ${languageName} speech into a written format.`;
  }
  return "<|audio|>can you transcribe the speech into a written format?";
}

function renderGranite41ChatPrompt(language) {
  return `USER: ${granite41Prompt(language || "")}\n ASSISTANT:`;
}

function loadTokenizer(modelPath) {
  return new Tokenizer(
    readJson(modelPath, "tokenizer.json"),
    readJson(modelPath, "tokenizer_config.json"),
  );
}

function cleanDecodedText(tokenizer, tokenIds) {
  return tokenizer
    .decode(tokenIds, {
      skip_special_tokens: true,
      clean_up_tokenization_spaces: false,
    })
    .replace(/\s+([,.;:!?])/g, "$1")
    .trim();
}

function ortExecutionProviders(device) {
  if (device === "webgpu") {
    return ["webgpu"];
  }
  if (device === "dml") {
    // onnxruntime-node ships the DirectML execution provider on Windows.
    // Nodes that DirectML cannot run fall back to CPU within the same
    // session, so this preserves correctness while using the GPU where it
    // can.
    return ["dml"];
  }
  if (device === "cpu") {
    return ["cpu"];
  }
  return ["cpu"];
}

async function createOrtSession(modelPath, relativePath, device) {
  return await ort.InferenceSession.create(join(modelPath, relativePath), {
    executionProviders: ortExecutionProviders(device),
  });
}

function causalMask4d(queryLength, keyLength, pastLength) {
  const mask = new Float32Array(queryLength * keyLength);
  for (let query = 0; query < queryLength; query += 1) {
    const allowedThrough = query + pastLength;
    for (let key = allowedThrough + 1; key < keyLength; key += 1) {
      mask[query * keyLength + key] = ORT_NEG_INF;
    }
  }
  return mask;
}

function sequentialPositionIds(length, offset = 0) {
  return BigInt64Array.from({ length }, (_, index) => BigInt(offset + index));
}

function spliceAudioEmbeddings(textIds, textEmbeds, audioEmbeds, audioLength) {
  const audioPositions = [];
  for (let index = 0; index < textIds.length; index += 1) {
    if (textIds[index] === GRANITE_4_1_AUDIO_TOKEN_ID) {
      audioPositions.push(index);
    }
  }
  if (audioPositions.length === 0) {
    throw new Error("Granite 4.1 prompt did not contain the <|audio|> token.");
  }
  if (audioPositions.length !== 1 && audioPositions.length !== audioLength) {
    throw new Error(
      `Granite 4.1 prompt has ${audioPositions.length} audio tokens, but encoder produced ${audioLength} audio embeddings.`,
    );
  }

  if (audioPositions.length === audioLength) {
    const output = new Float32Array(textIds.length * GRANITE_4_1_HIDDEN_SIZE);
    output.set(textEmbeds);
    for (let index = 0; index < audioLength; index += 1) {
      const tokenPosition = audioPositions[index];
      output.set(
        audioEmbeds.subarray(
          index * GRANITE_4_1_HIDDEN_SIZE,
          (index + 1) * GRANITE_4_1_HIDDEN_SIZE,
        ),
        tokenPosition * GRANITE_4_1_HIDDEN_SIZE,
      );
    }
    return { data: output, length: textIds.length };
  }

  const audioPosition = audioPositions[0];
  const outputLength = textIds.length - 1 + audioLength;
  const output = new Float32Array(outputLength * GRANITE_4_1_HIDDEN_SIZE);
  const prefixValues = audioPosition * GRANITE_4_1_HIDDEN_SIZE;
  output.set(textEmbeds.subarray(0, prefixValues), 0);
  output.set(audioEmbeds.subarray(0, audioLength * GRANITE_4_1_HIDDEN_SIZE), prefixValues);
  const suffixStart = (audioPosition + 1) * GRANITE_4_1_HIDDEN_SIZE;
  output.set(
    textEmbeds.subarray(suffixStart),
    (audioPosition + audioLength) * GRANITE_4_1_HIDDEN_SIZE,
  );
  return { data: output, length: outputLength };
}

function presentKvFromOutputs(outputs) {
  const past = [];
  for (let layer = 0; layer < GRANITE_4_1_NUM_LAYERS; layer += 1) {
    past.push(outputs[`present.${layer}.key`]);
    past.push(outputs[`present.${layer}.value`]);
  }
  return past;
}

function addPastKvFeeds(feeds, pastKv) {
  for (let layer = 0; layer < GRANITE_4_1_NUM_LAYERS; layer += 1) {
    feeds[`past_key_values.${layer}.key`] = pastKv[2 * layer];
    feeds[`past_key_values.${layer}.value`] = pastKv[2 * layer + 1];
  }
}

async function loadGranite41ArRuntime(options, device, webgpuAvailable) {
  const modelPath = modelPathForTransformers(options.modelPath);
  const precision = options.dtype || "int8";
  const graphRoot = precision === "int8" ? "int8" : precision;
  const accelerated = device === "webgpu" || device === "dml";
  const frontend = new Granite41AudioFrontend(
    readJson(modelPath, "preprocessor_config.json"),
  );
  const tokenizer = loadTokenizer(modelPath);
  const encoder = await createOrtSession(modelPath, `${graphRoot}/encoder.onnx`, device);
  const embedTokens = await createOrtSession(
    modelPath,
    `${graphRoot}/embed_tokens.onnx`,
    device,
  );
  const promptEncode = await createOrtSession(
    modelPath,
    `${graphRoot}/prompt_encode.onnx`,
    device,
  );
  const decodeStep = await createOrtSession(
    modelPath,
    `${graphRoot}/decode_step.onnx`,
    device,
  );

  async function transcribeChunk(audio, language, maxNewTokens) {
    const features = frontend.extract(audio);
    const encoderOutputs = await encoder.run({
      input_features: float32Tensor(
        features.inputFeatures,
        [1, features.featureFrames, 160],
      ),
    });
    const audioEmbedsTensor = encoderOutputs.audio_embeds;
    const audioSizeTensor = encoderOutputs.audio_embed_sizes;
    const audioLength = Math.min(
      Number(audioSizeTensor.data[0]),
      Number(audioEmbedsTensor.dims[1]),
    );

    const prompt = renderGranite41ChatPrompt(language);
    const textIds = tokenizer.encode(prompt, { add_special_tokens: true }).ids;
    const textEmbeds = await embedTokens.run({
      input_ids: int64Tensor(textIds, [1, textIds.length]),
    });
    const spliced = spliceAudioEmbeddings(
      textIds,
      textEmbeds.inputs_embeds.data,
      audioEmbedsTensor.data,
      audioLength,
    );
    const positionIds = sequentialPositionIds(spliced.length);
    const promptOutputs = await promptEncode.run({
      inputs_embeds: float32Tensor(
        spliced.data,
        [1, spliced.length, GRANITE_4_1_HIDDEN_SIZE],
      ),
      position_ids: new ort.Tensor("int64", positionIds, [1, spliced.length]),
      attention_mask: float32Tensor(
        causalMask4d(spliced.length, spliced.length, 0),
        [1, 1, spliced.length, spliced.length],
      ),
    });

    const generated = [];
    const promptLogits = promptOutputs.logits;
    const vocabSize = Number(promptLogits.dims[2]);
    generated.push(argmax(promptLogits.data, (spliced.length - 1) * vocabSize, vocabSize));
    let pastKv = presentKvFromOutputs(promptOutputs);

    for (let step = 1; step < (maxNewTokens || 1024); step += 1) {
      const previousToken = generated[generated.length - 1];
      if (previousToken === GRANITE_4_1_EOS_TOKEN_ID) {
        break;
      }
      const nextEmbeds = await embedTokens.run({
        input_ids: int64Tensor([previousToken], [1, 1]),
      });
      const pastLength = spliced.length + step - 1;
      const totalLength = pastLength + 1;
      const feeds = {
        inputs_embeds: nextEmbeds.inputs_embeds,
        position_ids: int64Tensor([pastLength], [1, 1]),
        attention_mask: float32Tensor(
          causalMask4d(1, totalLength, pastLength),
          [1, 1, 1, totalLength],
        ),
      };
      addPastKvFeeds(feeds, pastKv);
      const stepOutputs = await decodeStep.run(feeds);
      const stepLogits = stepOutputs.logits;
      const stepVocabSize = Number(stepLogits.dims[2]);
      generated.push(argmax(stepLogits.data, 0, stepVocabSize));
      pastKv = presentKvFromOutputs(stepOutputs);
    }

    return cleanDecodedText(tokenizer, generated);
  }

  return {
    device,
    gpuAvailable: accelerated,
    webgpuAvailable: webgpuAvailable || device === "webgpu",
    async transcribe(request) {
      const audioChunks = splitAudioAtQuietBoundaries(
        request.audio,
        TARGET_SAMPLE_RATE,
        GRANITE_MAX_CHUNK_SECONDS,
      );
      const chunkTexts = [];
      for (const chunk of audioChunks) {
        chunkTexts.push(
          await transcribeChunk(
            chunk,
            request.language || "",
            request.maxNewTokens,
          ),
        );
      }
      return joinTranscriptChunks(chunkTexts);
    },
  };
}

function ctcDraftTokenIds(bpeLogits, bpeMask) {
  // Greedy CTC decode of the encoder's BPE head, matching
  // GraniteSpeechNarForASR._ctc_collapse_decode: per-valid-frame argmax ->
  // collapse consecutive repeats -> drop the CTC blank -> shift to LLM ids.
  // The BPE head emits vocab+1 (=100353) classes with the blank at index 0, so
  // a non-blank class c is LLM token (c - 1). The collapsed ids feed the
  // editor's insertion slots *directly* (do NOT decode to text and re-encode:
  // re-tokenisation changes the id sequence/length and breaks the editor's
  // trained [eos, t0, eos, t1, ...] structure).
  const [, timesteps, vocabSize] = bpeLogits.dims.map(Number);
  const raw = [];
  for (let timestep = 0; timestep < timesteps; timestep += 1) {
    if (!bpeMask.data[timestep]) {
      continue;
    }
    raw.push(argmax(bpeLogits.data, timestep * vocabSize, vocabSize));
  }
  const collapsed = uniqueConsecutiveWithout(raw, GRANITE_4_1_NAR_CTC_BLANK_ID);
  return collapsed.map((id) => id - GRANITE_4_1_NAR_CTC_TOKEN_OFFSET);
}

function insertionSlotIds(tokenIds) {
  const outputLength = Math.max(2 * tokenIds.length + 1, 8);
  const output = new Array(outputLength).fill(GRANITE_4_1_EOS_TOKEN_ID);
  for (let index = 0; index < tokenIds.length; index += 1) {
    output[2 * index + 1] = tokenIds[index];
  }
  return output;
}

function buildNarInputs(audioEmbeds, audioLength, textEmbeds, textLength) {
  const totalLength = audioLength + textLength;
  const output = new Float32Array(totalLength * GRANITE_4_1_HIDDEN_SIZE);
  for (let index = 0; index < audioLength * GRANITE_4_1_HIDDEN_SIZE; index += 1) {
    output[index] = audioEmbeds[index] / GRANITE_4_1_NAR_EMBEDDING_MULTIPLIER;
  }
  output.set(
    textEmbeds.subarray(0, textLength * GRANITE_4_1_HIDDEN_SIZE),
    audioLength * GRANITE_4_1_HIDDEN_SIZE,
  );
  return output;
}

async function loadGranite41NarRuntime(options, device, webgpuAvailable) {
  const modelPath = modelPathForTransformers(options.modelPath);
  const precision = options.dtype || "int8";
  const graphRoot = precision === "int8" ? "int8" : precision;
  const accelerated = device === "webgpu" || device === "dml";
  const frontend = new Granite41AudioFrontend(
    readJson(modelPath, "preprocessor_config.json"),
  );
  const tokenizer = loadTokenizer(modelPath);
  const encoder = await createOrtSession(modelPath, `${graphRoot}/encoder.onnx`, device);
  const embedTokens = await createOrtSession(
    modelPath,
    `${graphRoot}/embed_tokens.onnx`,
    device,
  );
  const editor = await createOrtSession(modelPath, `${graphRoot}/editor.onnx`, device);

  async function transcribeChunk(audio) {
    const features = frontend.extract(audio);
    const encoderOutputs = await encoder.run({
      input_features: float32Tensor(
        features.inputFeatures,
        [1, features.featureFrames, 160],
      ),
      attention_mask: new ort.Tensor(
        "int64",
        features.attentionMask,
        [1, features.featureFrames],
      ),
    });
    const draftIds = ctcDraftTokenIds(
      encoderOutputs.bpe_logits_dense,
      encoderOutputs.bpe_mask,
    );
    const slotIds = insertionSlotIds(draftIds);
    const textEmbeds = await embedTokens.run({
      input_ids: int64Tensor(slotIds, [1, slotIds.length]),
    });
    const audioLength = Math.min(
      Number(encoderOutputs.audio_lengths.data[0]),
      Number(encoderOutputs.audio_embeds.dims[1]),
    );
    const inputsEmbeds = buildNarInputs(
      encoderOutputs.audio_embeds.data,
      audioLength,
      textEmbeds.inputs_embeds.data,
      slotIds.length,
    );
    const totalLength = audioLength + slotIds.length;
    const editorOutputs = await editor.run({
      inputs_embeds: float32Tensor(
        inputsEmbeds,
        [1, totalLength, GRANITE_4_1_HIDDEN_SIZE],
      ),
      position_ids: new ort.Tensor(
        "int64",
        sequentialPositionIds(totalLength),
        [1, totalLength],
      ),
      attention_mask: float32Tensor(
        new Float32Array(totalLength * totalLength),
        [1, 1, totalLength, totalLength],
      ),
    });
    const logits = editorOutputs.logits;
    const vocabSize = Number(logits.dims[2]);
    const predicted = [];
    for (let index = 0; index < slotIds.length; index += 1) {
      predicted.push(
        argmax(logits.data, (audioLength + index) * vocabSize, vocabSize),
      );
    }
    return cleanDecodedText(
      tokenizer,
      uniqueConsecutiveWithout(predicted, GRANITE_4_1_EOS_TOKEN_ID),
    );
  }

  return {
    device,
    gpuAvailable: accelerated,
    webgpuAvailable: webgpuAvailable || device === "webgpu",
    async transcribe(request) {
      const audioChunks = splitAudioAtQuietBoundaries(
        request.audio,
        TARGET_SAMPLE_RATE,
        GRANITE_MAX_CHUNK_SECONDS,
      );
      const chunkTexts = [];
      for (const chunk of audioChunks) {
        chunkTexts.push(await transcribeChunk(chunk));
      }
      return joinTranscriptChunks(chunkTexts);
    },
  };
}

async function transcribeGraniteChunk(processor, model, audio, language, maxNewTokens) {
  const messages = [
    {
      role: "user",
      content: granitePrompt(language || ""),
    },
  ];
  const prompt = processor.apply_chat_template(messages, {
    add_generation_prompt: false,
    tokenize: false,
  });
  const inputs = await processor(prompt, audio);
  const generatedIds = await model.generate({
    ...inputs,
    max_new_tokens: maxNewTokens || 1024,
  });
  const inputLength = inputs.input_ids.dims.at(-1);
  const generatedTexts = processor.batch_decode(
    generatedIds.slice(null, [inputLength, null]),
    { skip_special_tokens: true },
  );
  return String(generatedTexts?.[0] || "");
}

async function loadRuntimeForDevice(options, device, webgpuAvailable) {
  const modelPath = modelPathForTransformers(options.modelPath);
  const accelerated = ["webgpu", "dml"].includes(device);
  const runtimeWebGpuAvailable = webgpuAvailable || device === "webgpu";

  if (options.model === "cohere-transcribe-03-2026") {
    const transcriber = await pipeline(
      "automatic-speech-recognition",
      modelPath,
      { dtype: options.dtype, device },
    );
    return {
      device,
      gpuAvailable: accelerated,
      webgpuAvailable: runtimeWebGpuAvailable,
      async transcribe(request) {
        const audio = request.audio;
        const result = await transcriber(audio, {
          max_new_tokens: request.maxNewTokens || 1024,
          language: request.language || "en",
        });
        return typeof result === "string" ? result : String(result?.text || "");
      },
    };
  }

  if (GRANITE_PIPELINE_MODELS.has(options.model)) {
    const processor = await AutoProcessor.from_pretrained(modelPath);
    const model = await GraniteSpeechForConditionalGeneration.from_pretrained(
      modelPath,
      { dtype: graniteDtype(options.dtype), device },
    );
    return {
      device,
      gpuAvailable: accelerated,
      webgpuAvailable: runtimeWebGpuAvailable,
      async transcribe(request) {
        const audio = request.audio;
        const audioChunks = splitAudioAtQuietBoundaries(
          audio,
          TARGET_SAMPLE_RATE,
          GRANITE_MAX_CHUNK_SECONDS,
        );
        const chunkTexts = [];
        for (const chunk of audioChunks) {
          chunkTexts.push(
            await transcribeGraniteChunk(
              processor,
              model,
              chunk,
              request.language || "",
              request.maxNewTokens,
            ),
          );
        }
        return joinTranscriptChunks(chunkTexts);
      },
    };
  }

  const granite41Layout = GRANITE_4_1_MODEL_LAYOUTS.get(options.model);
  if (granite41Layout === "granite_4_1_ar") {
    return await loadGranite41ArRuntime(options, device, webgpuAvailable);
  }
  if (granite41Layout === "granite_4_1_nar") {
    return await loadGranite41NarRuntime(options, device, webgpuAvailable);
  }

  throw new Error(`Unsupported model: ${options.model}`);
}

async function loadRuntime(options) {
  const webgpuAvailable = await hasWebGpuAdapter();
  const candidateDevices = resolveDevice(options.device);
  const errors = [];
  for (let index = 0; index < candidateDevices.length; index += 1) {
    const device = candidateDevices[index];
    try {
      return {
        runtime: await loadRuntimeForDevice(options, device, webgpuAvailable),
        candidateDevices,
        index,
        webgpuAvailable,
        fallbackErrors: errors,
      };
    } catch (error) {
      errors.push(`${device}: ${conciseError(error)}`);
    }
  }
  throw new Error(
    `Failed to load ${options.model}. Tried devices: ${candidateDevices.join(", ")}\n\n${errors.join("\n\n")}`,
  );
}

async function runServer(options) {
  let runtime;
  let candidateDevices = [];
  let runtimeIndex = -1;
  let webgpuAvailable = false;
  let fallbackErrors = [];
  try {
    await loadRuntimeDependencies();
    const loaded = await loadRuntime(options);
    runtime = loaded.runtime;
    candidateDevices = loaded.candidateDevices;
    runtimeIndex = loaded.index;
    webgpuAvailable = loaded.webgpuAvailable;
    fallbackErrors = loaded.fallbackErrors;
    writeJson({
      type: "ready",
      ok: true,
      model: options.model,
      device: runtime.device,
      gpuAvailable: runtime.gpuAvailable,
      webgpuAvailable: runtime.webgpuAvailable,
      fallbackErrors,
    });
  } catch (error) {
    writeJson({ type: "ready", ok: false, error: formatError(error) });
    process.exitCode = 1;
    return;
  }

  async function transcribeWithFallback(request) {
    const audio = decodeWavFile(request.audioPath, TARGET_SAMPLE_RATE);
    const preparedRequest = { ...request, audio };
    try {
      return {
        text: await runtime.transcribe(preparedRequest),
        fallbackErrors,
      };
    } catch (error) {
      if (!["auto", "gpu"].includes(options.device)) {
        throw error;
      }
      const errors = [`${runtime.device}: ${conciseError(error)}`];
      for (let index = runtimeIndex + 1; index < candidateDevices.length; index += 1) {
        const nextDevice = candidateDevices[index];
        try {
          runtime = await loadRuntimeForDevice(options, nextDevice, webgpuAvailable);
          runtimeIndex = index;
          fallbackErrors = [...fallbackErrors, ...errors];
          return {
            text: await runtime.transcribe(preparedRequest),
            fallbackErrors,
          };
        } catch (nextError) {
          errors.push(`${nextDevice}: ${conciseError(nextError)}`);
        }
      }
      throw new Error(
        `ONNX runtime failed during transcription on all fallback devices.\n\n${errors.join("\n\n")}`,
      );
    }
  }

  // The async iterator provides strict request serialization. A second stdin
  // line cannot enter inference or mutate fallback runtime state until the
  // first request has produced its response.
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const rawLine of lines) {
    let request;
    try {
      request = parseProtocolRequestLine(rawLine);
    } catch (error) {
      writeJson({ ok: false, error: formatError(error) });
      continue;
    }
    if (request === null) {
      continue;
    }

    if (request.command === "shutdown") {
      break;
    }

    if (request.command !== "transcribe") {
      writeJson({
        id: request.id,
        ok: false,
        error: `Unsupported command: ${request.command}`,
      });
      continue;
    }

    try {
      const result = await transcribeWithFallback(request);
      writeJson({
        id: request.id,
        ok: true,
        text: result.text,
        device: runtime.device,
        gpuAvailable: runtime.gpuAvailable,
        webgpuAvailable: runtime.webgpuAvailable,
        fallbackErrors: result.fallbackErrors,
      });
    } catch (error) {
      writeJson({ id: request.id, ok: false, error: formatError(error) });
    }
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.server) {
    writeJson({ ok: false, error: "Only --server mode is supported." });
    process.exitCode = 2;
    return;
  }

  await runServer(args);
}

if (
  !process.execArgv.includes("-e") &&
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  await main();
}
