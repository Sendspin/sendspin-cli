#!/usr/bin/env bash
# Script to build (and optionally push) multi-platform docker images.

# Stop on errors
set -e

# Change directory to the repository root
cd "$(dirname "$0")/.."

# Check if docker is installed
if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed." >&2
    exit 1
fi

# Parse arguments
TAG="latest"
PUSH=false
IMAGE_NAME="ghcr.io/shardshunt/sendspin-cli-for-sendspin-kodi"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --push) PUSH=true ;;
        *) TAG="$1" ;;
    esac
    shift
done

FULL_IMAGE="${IMAGE_NAME}:${TAG}"

echo "=========================================================="
echo "Preparing multi-platform build for: ${FULL_IMAGE}"
echo "Platforms: linux/amd64, linux/arm64, linux/arm/v7, linux/386"
echo "=========================================================="

# 1. Register emulation support on the host kernel
echo "Checking/Registering emulation binaries..."
sudo docker run --privileged --rm tonistiigi/binfmt --install all

# 2. Setup buildx builder
BUILDER_NAME="sendspin-builder"
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    echo "Creating new buildx builder: ${BUILDER_NAME}..."
    docker buildx create --name "$BUILDER_NAME" --use
else
    echo "Using existing buildx builder: ${BUILDER_NAME}..."
    docker buildx use "$BUILDER_NAME"
fi

# Bootstrap the builder to ensure it is running and active
docker buildx inspect --bootstrap

# 3. Execute the build
BUILD_ARGS=(
    --platform linux/amd64,linux/arm64,linux/arm/v7,linux/386
    -t "$FULL_IMAGE"
    .
)

if [ "$PUSH" = true ]; then
    echo "Building and pushing to registry..."
    docker buildx build "${BUILD_ARGS[@]}" --push
else
    echo "Building locally (dry-run, results will remain in cache)..."
    echo "Run with '--push' to upload to the registry."
    docker buildx build "${BUILD_ARGS[@]}"
fi

echo "Done!"
