#pragma once

#include "CoreMinimal.h"
#include "Async/Future.h"

// =============================================================================
// EPayloadType
//
//   Bounds  — sent by BoundingBoxFinder; carries the computational-space BB.
//             UE stores it immediately and ACKs on receipt.
//   Mesh    — sent by MeshSender for each frame / static mesh.
//             UE buffers it and ACKs on receipt.
//   Update  — sent by MeshSender after all meshes are transmitted.
//             UE applies the retained BB to every buffered payload, builds all
//             meshes, then ACKs on completion (not on receipt).
// =============================================================================

enum class EPayloadType : uint8
{
    Mesh,
    Bounds,
    Update,
};

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

    /** Distinguishes bounds / mesh / update messages. */
    EPayloadType PayloadType = EPayloadType::Mesh;

    // ---- Bounds fields (PayloadType == Bounds) ------------------------------

    /** Bounding box of the full computational space in ParaView source units. */
    FVector BoundsMin = FVector::ZeroVector;
    FVector BoundsMax = FVector::ZeroVector;

    // ---- Update completion signal (PayloadType == Update) -------------------

    /**
     * Set by the socket thread before enqueuing an Update payload.
     * The game thread calls SetValue(0) after all buffered meshes are built,
     * allowing the socket thread to send the ACK only once processing is done.
     */
    TSharedPtr<TPromise<int32>> CompletionPromise;

    // ---- Mesh fields (PayloadType == Mesh) ----------------------------------

    /** Routing identifier — matched to a MeshActor key. */
    FString MeshId = TEXT("default");

    /** 0-based index into the alphabetically-sorted list of ADisplayVolumeActors
     *  by VolumeName.  Set by the ParaView plugin from the dropdown selection —
     *  users see names, never this number. */
    int32 VolumeId = 0;

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

    /**
     * Global bounding box — stamped onto buffered Mesh payloads by
     * UMeshBuilderSubsystem when the Update message is processed.
     * Tells AMeshActor the full computational-space extent so it computes a
     * consistent normalization transform across all frames.
     * Never transmitted on the wire; always filled in on the game thread.
     */
    bool    bHasAnimBounds = false;
    FVector AnimBoundsMin  = FVector::ZeroVector;
    FVector AnimBoundsMax  = FVector::ZeroVector;

    // =========================================================================
    // Computed by AMeshActor (not transmitted)
    // =========================================================================

    /** Smooth per-vertex normals, computed from Vertices + Triangles. */
    TArray<FVector> Normals;

    /** Per-vertex UVs derived from Scalars and the scalar range. */
    TArray<FVector2D> UVs;
};
