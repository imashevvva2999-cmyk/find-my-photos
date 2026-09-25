#!/bin/bash
# Render build step: install the exact library versions and download the face models
# (verified by SHA-256, same as run.sh). Runs on Render's servers, not on the Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
pip install --no-cache-dir -r requirements.txt

ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
fetch_model() {  # name, url, sha256
  local file="models/$1"
  mkdir -p models
  if [ ! -f "$file" ]; then
    curl -fsSL -o "$file.part" "$2"
    mv "$file.part" "$file"
  fi
  if [ "$(sha256sum "$file" | cut -d' ' -f1)" != "$3" ]; then
    echo "Model $1 does not match its expected checksum."; rm -f "$file"; exit 1
  fi
}
fetch_model face_detection_yunet_2023mar.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch_model face_recognition_sface_2021dec.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
echo "Build finished."
