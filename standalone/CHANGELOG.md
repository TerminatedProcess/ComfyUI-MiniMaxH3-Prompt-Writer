# Standalone changelog

## Unreleased

- **Restarting no longer moves the studio to a different port.** Stopping a server leaves its connections in TIME_WAIT for about a minute, and the check for "is my port free?" was refused for that whole window — so restarting within a minute of stopping, which is what a restart is, found the port occupied and quietly relocated to a random one. The bookmark then pointed at nothing and the studio looked like it had failed to start. The check now matches what the server itself does, so the port you asked for is the port you get. If it genuinely is taken, the move is announced instead of silent, and the startup banner is flushed so a service log always records where the studio actually went.

## 0.1.7 - 2026-09-11

- Fixed video trimming and cropping failing because NumPy was missing.
- Existing Windows environments now install missing dependencies on launch.

## 0.1.6 - 2026-09-10

- Fixed a Local GGUF crash when processing large images with vision models.
- Added server log details when a Local GGUF connection is interrupted.

## 0.1.5 - 2026-09-09

- Added **Sequence mode**: write a series of timed clips from one brief, keep action and references consistent, and edit each prompt separately.
- Added **Compact mode** for Sequence.
- Improved draft saving, model switching, generation reliability and video playback. Added text-only Music 3 and a Linux launcher for source installs.
- Updated the shared Writer core to `0.4.6`.

## 0.1.4 - 2026-09-06

- Updated to the shared H3 Prompt Writer `0.4.5` interface and core.
- Added Media Composer for collages and Media Editor for crop and trim.
- Added Light theme and adjustable Interface Size.
- Added model selection and unload controls for compatible external llama.cpp routers.
- Kept ComfyUI-only workflow and memory controls out of Standalone.

## 0.1.3 - 2026-09-02

- Updated the shared Writer interface and core to H3 Prompt Writer extension `0.4.4`.
- Added a compact Clear menu for clearing prompts while keeping media, or clearing the entire workspace.
- Added custom 2–16 frame contact sheets with more readable frame labels.
- Improved safe remote Ollama host editing and private-network hostname handling.

## 0.1.2 - 2026-08-30

- Fixed Local GGUF generation failing before the model could respond.
- Restored Thinking controls for Local GGUF models whose templates support them.

## 0.1.1 - 2026-08-29

- Updated the shared Writer interface and core to H3 Prompt Writer extension `0.4.3`.
- Added Local GGUF Custom Context, KV cache, Generation budget, and supported reasoning effort controls.
- Improved metadata-based model and projector detection for renamed GGUF files.
- Increased the video Creative Brief limit to 8,000 characters.
- Kept External llama.cpp reasoning settings under server control.
- Fixed Local GGUF generation-budget handling.

## 0.1.0 - 2026-08-27

- First Windows Standalone release candidate.
- Reuses the shared H3 Prompt Writer core without requiring ComfyUI.
- Supports Ollama, API providers, External llama.cpp, and existing local GGUF models.
- Manages a user-selected `llama-server.exe` outside the Python process.
- Supports GGUF vision projectors, combined model locations, and metadata-based pairing.
- Based on H3 Prompt Writer extension `0.4.2`.
