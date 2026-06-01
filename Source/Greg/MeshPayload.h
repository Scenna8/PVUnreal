#pragma once

#include "CoreMinimal.h"

/**
 * FMeshPayload
 *
 * Plain data container for one complete mesh update.
 * Lives in its own header to avoid circular dependencies.
 * No UE macros — plain C++.
 *
 * Fields are split into two groups:
 *   Wire data  — received from Python over TCP.
 *   Computed   — filled in by AMeshActor after receipt, never transmitted.
 */
struct FMeshPayload
{
    // =========================================================================
    // Wire data (received from Python)
    // =========================================================================

    /** Routing identifier — matched to a MeshActor key. */
    FString MeshId = TEXT("default");

    /** Which ADisplayVolumeActor to place this mesh inside. */
    int32 VolumeId = 1;

    /** -1 = static mesh.  >= 0 = one frame of an animation sequence. */
    int32 FrameIndex  = -1;
    /** Total expected frames in this sequence; -1 = unknown. */
    int32 TotalFrames = -1;
    /** Target playback rate for animation. */
    float PlaybackFPS = 24.f;

    /** Raw 3D positions in whatever coordinate units the sender uses.
     *  AMeshActor normalises these to ±100 cm before rendering. */
    TArray<FVector> Vertices;

    /** Flat triangle index list; every 3 ints define one triangle. */
    TArray<int32> Triangles;

    /** Per-vertex scalar values.  Empty means no colour data.
     *  AMeshActor maps these to UV.X ∈ [0,1] using ScalarMin / ScalarMax. */
    TArray<float> Scalars;

    /** Scalar value that maps to UV.X = 0 (first colormap entry). */
    float ScalarMin = 0.f;
    /** Scalar value that maps to UV.X = 1 (last colormap entry). */
    float ScalarMax = 1.f;

    /** RGBA8 colormap texture pixels (Width × Height × 4 bytes, row-major).
     *  Built Python-side from ParaView's CTF or matplotlib. */
    TArray<uint8> ColorMapData;
    int32         ColorMapWidth  = 0;
    int32         ColorMapHeight = 0;

    // =========================================================================
    // Computed by AMeshActor (not transmitted)
    // =========================================================================

    /** Smooth per-vertex normals, computed from Vertices + Triangles. */
    TArray<FVector> Normals;

    /** Per-vertex UVs derived from Scalars and the scalar range. */
    TArray<FVector2D> UVs;
};
