import {
  AutoProcessor,
  GraniteSpeechForConditionalGeneration,
  env,
  pipeline,
  read_audio,
} from "@huggingface/transformers";

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

function resolveDevice(requestedDevice, webgpuAvailable) {
  if (requestedDevice && requestedDevice !== "auto") {
    return [requestedDevice];
  }
  const devices = [];
  if (webgpuAvailable) {
    devices.push("webgpu");
  }
  if (process.platform === "win32") {
    devices.push("dml");
  }
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
  const accelerated = device === "webgpu" || device === "dml";

  if (options.model === "cohere-transcribe-03-2026") {
    const transcriber = await pipeline(
      "automatic-speech-recognition",
      modelPath,
      { dtype: options.dtype, device },
    );
    return {
      device,
      gpuAvailable: accelerated,
      webgpuAvailable,
      async transcribe(request) {
        const result = await transcriber(request.audioPath, {
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
      webgpuAvailable,
      async transcribe(request) {
        const audio = await read_audio(request.audioPath, 16000);
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
  const candidateDevices = resolveDevice(options.device, webgpuAvailable);
  const errors = [];
  for (const device of candidateDevices) {
    try {
      return await loadRuntimeForDevice(options, device, webgpuAvailable);
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
  try {
    runtime = await loadRuntime(options);
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
        const text = await runtime.transcribe(request);
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
