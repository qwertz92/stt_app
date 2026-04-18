import {
  AutoProcessor,
  GraniteSpeechForConditionalGeneration,
  env,
  pipeline,
} from "@huggingface/transformers";
import { readFileSync } from "node:fs";

env.allowLocalModels = true;
env.allowRemoteModels = false;
env.useBrowserCache = false;
env.useFSCache = true;

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

function modelPathForTransformers(modelPath) {
  return String(modelPath || "").replaceAll("\\", "/");
}

function readAscii(buffer, offset, length) {
  return buffer.toString("ascii", offset, offset + length);
}

function findChunk(buffer, chunkId) {
  let offset = 12;
  while (offset + 8 <= buffer.length) {
    const id = readAscii(buffer, offset, 4);
    const size = buffer.readUInt32LE(offset + 4);
    const dataOffset = offset + 8;
    if (id === chunkId) {
      return { offset: dataOffset, size };
    }
    offset = dataOffset + size + (size % 2);
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

function decodeWavFile(audioPath, targetSampleRate) {
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

  const fmt = findChunk(buffer, "fmt ");
  const data = findChunk(buffer, "data");
  if (!fmt || !data) {
    throw new Error("Invalid WAV file: missing fmt or data chunk.");
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

  const bytesPerSample = Math.ceil(bitsPerSample / 8);
  const frameCount = Math.floor(data.size / blockAlign);
  const mono = new Float32Array(frameCount);
  const isPcm = audioFormat === 1 || audioFormat === 65534;
  const isFloat = audioFormat === 3;
  if (!isPcm && !isFloat) {
    throw new Error(`Unsupported WAV encoding: ${audioFormat}. Use PCM or float WAV.`);
  }

  for (let frame = 0; frame < frameCount; frame += 1) {
    const frameOffset = data.offset + frame * blockAlign;
    let sum = 0;
    for (let channel = 0; channel < channelCount; channel += 1) {
      const sampleOffset = frameOffset + channel * bytesPerSample;
      const sample = isFloat
        ? decodeFloatSample(buffer, sampleOffset, bitsPerSample)
        : decodePcmSample(buffer, sampleOffset, bitsPerSample);
      sum += Number.isFinite(sample) ? sample : 0;
    }
    mono[frame] = Math.max(-1, Math.min(1, sum / channelCount));
  }

  return resampleLinear(mono, sampleRate, targetSampleRate);
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

  if (options.model === "granite-4.0-1b-speech") {
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
        const messages = [
          {
            role: "user",
            content: "<|audio|>can you transcribe the speech into a written format?",
          },
        ];
        const prompt = processor.apply_chat_template(messages, {
          add_generation_prompt: false,
          tokenize: false,
        });
        const inputs = await processor(prompt, audio);
        const generatedIds = await model.generate({
          ...inputs,
          max_new_tokens: request.maxNewTokens || 1024,
        });
        const inputLength = inputs.input_ids.dims.at(-1);
        const generatedTexts = processor.batch_decode(
          generatedIds.slice(null, [inputLength, null]),
          { skip_special_tokens: true },
        );
        return String(generatedTexts?.[0] || "");
      },
    };
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
      };
    } catch (error) {
      errors.push(`${device}: ${formatError(error)}`);
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
  try {
    const loaded = await loadRuntime(options);
    runtime = loaded.runtime;
    candidateDevices = loaded.candidateDevices;
    runtimeIndex = loaded.index;
    webgpuAvailable = loaded.webgpuAvailable;
    writeJson({
      type: "ready",
      ok: true,
      model: options.model,
      device: runtime.device,
      gpuAvailable: runtime.gpuAvailable,
      webgpuAvailable: runtime.webgpuAvailable,
    });
  } catch (error) {
    writeJson({ type: "ready", ok: false, error: formatError(error) });
    process.exitCode = 1;
    return;
  }

  async function transcribeWithFallback(request) {
    const audio = decodeWavFile(request.audioPath, 16000);
    const preparedRequest = { ...request, audio };
    try {
      return await runtime.transcribe(preparedRequest);
    } catch (error) {
      if (!["auto", "gpu"].includes(options.device)) {
        throw error;
      }
      const errors = [`${runtime.device}: ${formatError(error)}`];
      for (let index = runtimeIndex + 1; index < candidateDevices.length; index += 1) {
        const nextDevice = candidateDevices[index];
        try {
          runtime = await loadRuntimeForDevice(options, nextDevice, webgpuAvailable);
          runtimeIndex = index;
          return await runtime.transcribe(preparedRequest);
        } catch (nextError) {
          errors.push(`${nextDevice}: ${formatError(nextError)}`);
        }
      }
      throw new Error(
        `ONNX runtime failed during transcription on all fallback devices.\n\n${errors.join("\n\n")}`,
      );
    }
  }

  let buffer = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", async (chunk) => {
    buffer += chunk;
    while (buffer.includes("\n")) {
      const newlineIndex = buffer.indexOf("\n");
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (!line) {
        continue;
      }

      let request;
      try {
        request = JSON.parse(line);
      } catch (error) {
        writeJson({ ok: false, error: `Invalid JSON request: ${formatError(error)}` });
        continue;
      }

      if (request.command === "shutdown") {
        process.exit(0);
        return;
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
        const text = await transcribeWithFallback(request);
        writeJson({
          id: request.id,
          ok: true,
          text,
          device: runtime.device,
          gpuAvailable: runtime.gpuAvailable,
          webgpuAvailable: runtime.webgpuAvailable,
        });
      } catch (error) {
        writeJson({ id: request.id, ok: false, error: formatError(error) });
      }
    }
  });
}

const args = parseArgs(process.argv.slice(2));
if (!args.server) {
  writeJson({ ok: false, error: "Only --server mode is supported." });
  process.exit(2);
}

await runServer(args);
