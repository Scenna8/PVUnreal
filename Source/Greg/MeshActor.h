#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "ProceduralMeshComponent.h"
#include "Containers/Ticker.h"
#include "MeshActor.generated.h"

struct FMeshPayload;
class UTexture2D;
class UMaterialInstanceDynamic;

/**
 * AMeshActor
 *
 * Owns a UProceduralMeshComponent and applies incoming geometry and color maps
 * on demand. Also supports animated sequences: call AddAnimationFrame() for
 * each frame; playback starts automatically once all expected frames arrive.
 */
UCLASS()
class GREG_API AMeshActor : public AActor
{
    GENERATED_BODY()

public:
    AMeshActor();
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

    /**
     * Set the half-extents of the target DisplayVolumeActor box (in cm).
     * Must be called before ApplyMesh / AddAnimationFrame so the normalization
     * transform fits the ParaView bounding box into this UE box while
     * preserving the data's aspect ratio.
     * Default (100, 100, 100) gives the previous ±100 cm cube behaviour.
     */
    void SetTargetBoxExtents(const FVector& HalfExtents);

    /** Replace the current static mesh and color map. Game thread only. */
    void ApplyMesh(const FMeshPayload& Payload);

    /**
     * Buffer one animation frame. Playback starts automatically when
     * Payload.TotalFrames have been received.
     */
    void AddAnimationFrame(const FMeshPayload& Payload);

    /** Begin looped playback at the given frame rate. */
    void StartPlayback(float FPS);

    /** Stop the playback ticker (does not clear buffered frames). */
    void StopPlayback();

private:
    // ---- Rendering ---------------------------------------------------------

    UPROPERTY(VisibleAnywhere)
    UProceduralMeshComponent* ProcMesh;

    UPROPERTY()
    UMaterialInstanceDynamic* DynamicMaterial = nullptr;

    UPROPERTY()
    UTexture2D* ColorMapTexture = nullptr;

    // ---- Animation ---------------------------------------------------------

    /**
     * Pre-allocated storage for animation frames, indexed by FrameIndex.
     * Sized to TotalFrames on the first arriving payload so out-of-order
     * deliveries land in the correct slot.
     */
    TArray<FMeshPayload> AnimationFrames;

    /**
     * Pre-built colormap textures — one per frame, created during
     * AddAnimationFrame so playback never allocates on the render path.
     */
    UPROPERTY()
    TArray<UTexture2D*> FrameTextures;

    int32 ExpectedFrameCount  = 0;
    /** Frames actually received so far (distinct from AnimationFrames.Num()
     *  which is the pre-allocated capacity). */
    int32 ReceivedFrameCount  = 0;
    int32 CurrentFrameIdx     = 0;
    float FrameAccumulator    = 0.f;
    float SecondsPerFrame     = 1.f / 24.f;
    bool  bIsPlaying          = false;
    bool  bSectionCreated     = false;

    // Topology of the currently uploaded mesh section.
    int32 CurrentVertexCount   = 0;
    int32 CurrentTriangleCount = 0;

    FTSTicker::FDelegateHandle PlaybackHandle;

    bool TickPlayback(float DeltaTime);

    // ---- Normalization ------------------------------------------------------

    /**
     * Half-extents of the target DisplayVolumeActor box (cm).
     * Normalization maps the ParaView bounding box into this box with a single
     * uniform scale, so the data's aspect ratio is preserved.
     */
    FVector TargetHalfExtents = FVector(100.f);

    /**
     * Center and uniform-scale transform locked to a specific frame.
     * For static meshes this is recomputed on every ApplyMesh call.
     * For animations it is locked to frame 0 and reused for all subsequent
     * frames so the mesh doesn't jump or rescale between frames.
     */
    bool    bNormLocked = false;
    FVector NormCenter  = FVector::ZeroVector;
    float   NormScale   = 1.0f;

    /**
     * When true, ApplyFixedBounds() forces ProcMesh->Bounds to this fixed
     * local-space box after every CreateMeshSection_LinearColor call.
     * Prevents UProceduralMeshComponent from recomputing per-frame bounds
     * from actual vertex positions (which change shape each frame and cause
     * the bounding box to shift visually).
     */
    bool    bHasFixedBounds   = false;
    FVector FixedBoundsMin    = FVector::ZeroVector;
    FVector FixedBoundsMax    = FVector::ZeroVector;

    /** Compute NormCenter / NormScale from Vertices and set bNormLocked. */
    void LockNormalization(const TArray<FVector>& Vertices);

    /**
     * Compute NormCenter / NormScale from an explicit global bounding box
     * (in source units) and set bNormLocked.  Also computes FixedBoundsMin /
     * FixedBoundsMax — the post-normalization local box — so ApplyFixedBounds()
     * can lock the ProceduralMeshComponent to a stable bounding box.
     */
    void LockNormalizationFromBounds(const FVector& BoundsMin, const FVector& BoundsMax);

    /**
     * Override ProcMesh->Bounds with the pre-computed fixed box.
     * Call immediately after every CreateMeshSection_LinearColor so the
     * engine never sees the per-frame recomputed bounds.
     * No-op when bHasFixedBounds is false.
     */
    void ApplyFixedBounds() const;

    /** Apply the locked center+scale transform to every vertex in-place. */
    void NormalizeVertices(TArray<FVector>& Vertices) const;

    /** Compute smooth per-vertex normals from a triangle soup.
     *  Face normals (weighted by area) are accumulated at each vertex then
     *  normalised to unit length.  An outward-orientation check flips all
     *  normals if they point on average toward the centroid. */
    static TArray<FVector> ComputeSmoothNormals(
        const TArray<FVector>& Verts, const TArray<int32>& Tris);

    /** Map per-vertex scalar values to UV.X ∈ [0,1] using the given range.
     *  UV.Y is always 0.5 (samples the centre row of a 1-D colormap strip). */
    static TArray<FVector2D> ScalarsToUVs(
        const TArray<float>& Scalars, float VMin, float VMax);

    // ---- Helpers -----------------------------------------------------------

    /** Upload RGBA8 bytes into a new transient UTexture2D. */
    UTexture2D* BuildTexture(int32 Width, int32 Height,
                             const TArray<uint8>& Data) const;

    /** Set the ColorMap material parameter to the given texture. */
    void ApplyTexture(UTexture2D* Texture);
};
