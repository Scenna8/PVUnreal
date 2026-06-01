#include "MeshActor.h"
#include "MeshPayload.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Engine/Texture2D.h"

AMeshActor::AMeshActor()
{
    PrimaryActorTick.bCanEverTick = false;

    ProcMesh = CreateDefaultSubobject<UProceduralMeshComponent>(TEXT("ProcMesh"));
    RootComponent = ProcMesh;
    ProcMesh->SetCastShadow(false);

    static ConstructorHelpers::FObjectFinder<UMaterial> TwoSidedMat(
        TEXT("/Game/Materials/M_MeshViewer.M_MeshViewer"));
    if (TwoSidedMat.Succeeded())
    {
        ProcMesh->SetMaterial(0, TwoSidedMat.Object);
        UE_LOG(LogTemp, Warning, TEXT("MeshActor: Material loaded successfully"));
    }
    else
    {
        UE_LOG(LogTemp, Error, TEXT("MeshActor: Failed to load M_MeshViewer — check asset path"));
    }
}

void AMeshActor::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    StopPlayback();
    Super::EndPlay(EndPlayReason);
}

// =============================================================================
// Static mesh
// =============================================================================

void AMeshActor::ApplyMesh(const FMeshPayload& Payload)
{
    // ---- Color map → dynamic texture ----
    if (Payload.ColorMapWidth > 0 && Payload.ColorMapHeight > 0 &&
        Payload.ColorMapData.Num() == Payload.ColorMapWidth * Payload.ColorMapHeight * 4)
    {
        // Always create a fresh texture so the material parameter pointer changes,
        // guaranteeing the render thread samples the new data.
        UTexture2D* NewTex = BuildTexture(
            Payload.ColorMapWidth, Payload.ColorMapHeight, Payload.ColorMapData);
        ApplyTexture(NewTex);

        UE_LOG(LogTemp, Warning, TEXT("MeshActor: Color map applied (%dx%d)"),
            Payload.ColorMapWidth, Payload.ColorMapHeight);
    }

    // ---- Geometry — normalize, compute normals, compute UVs ----
    // If the sender supplied global bounds, lock the normalization transform
    // from them on the first call and reuse it for every subsequent frame.
    // This keeps the mesh stable when scrubbing through an animation.
    // Without bounds, recompute from this frame's vertices (static behavior).
    if (Payload.bHasAnimBounds)
    {
        if (!bNormLocked)
            LockNormalizationFromBounds(Payload.AnimBoundsMin, Payload.AnimBoundsMax);
        // else: already locked — reuse the existing transform
    }
    else
    {
        bNormLocked = false;
        LockNormalization(Payload.Vertices);
    }

    TArray<FVector> NormVerts = Payload.Vertices;
    NormalizeVertices(NormVerts);

    const TArray<FVector>   Normals = ComputeSmoothNormals(NormVerts, Payload.Triangles);
    const TArray<FVector2D> UVs     = Payload.Scalars.Num() == NormVerts.Num()
        ? ScalarsToUVs(Payload.Scalars, Payload.ScalarMin, Payload.ScalarMax)
        : TArray<FVector2D>{};

    ProcMesh->ClearMeshSection(0);
    ProcMesh->CreateMeshSection_LinearColor(
        0,
        NormVerts,
        Payload.Triangles,
        Normals,
        UVs,
        {},     // vertex colours (unused)
        {},     // tangents
        false   // no collision needed for visualization
    );

    bSectionCreated = true;
}

// =============================================================================
// Animation
// =============================================================================

void AMeshActor::AddAnimationFrame(const FMeshPayload& Payload)
{
    if (Payload.FrameIndex < 0) return;

    // Grab TotalFrames from whichever packet carries it.
    if (Payload.TotalFrames > 0)
        ExpectedFrameCount = Payload.TotalFrames;

    // Pre-allocate arrays to the full sequence length on the first call that
    // knows TotalFrames, so any frame can land at its correct index regardless
    // of arrival order.
    if (ExpectedFrameCount > 0 && AnimationFrames.Num() < ExpectedFrameCount)
    {
        AnimationFrames.SetNum(ExpectedFrameCount);  // default-constructed payloads
        FrameTextures.SetNum(ExpectedFrameCount);    // nullptrs
    }

    // Guard against an index that somehow exceeds capacity.
    if (!AnimationFrames.IsValidIndex(Payload.FrameIndex))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("MeshActor: FrameIndex %d out of range (capacity %d) — dropping"),
            Payload.FrameIndex, AnimationFrames.Num());
        return;
    }

    // Frame 0 starts a new sequence — reset the normalization lock so the
    // transform is recomputed from this sequence's geometry, not a stale one.
    if (Payload.FrameIndex == 0)
        bNormLocked = false;

    // Lock normalization once per sequence.
    // Prefer the global animation bounds supplied by the sender (covering all
    // frames in source units) over frame 0's local vertex bounds.  Using a
    // per-frame or per-sequence-start bounds causes the bounding box to shift
    // between frames when the contour changes shape.
    if (!bNormLocked)
    {
        if (Payload.bHasAnimBounds)
            LockNormalizationFromBounds(Payload.AnimBoundsMin, Payload.AnimBoundsMax);
        else
            LockNormalization(Payload.Vertices);
    }

    // Normalize a copy, then compute normals + UVs so playback never
    // re-derives geometry data from scratch on every frame advance.
    FMeshPayload NormPayload = Payload;
    NormalizeVertices(NormPayload.Vertices);

    NormPayload.Normals = ComputeSmoothNormals(NormPayload.Vertices, NormPayload.Triangles);
    if (NormPayload.Scalars.Num() == NormPayload.Vertices.Num())
        NormPayload.UVs = ScalarsToUVs(
            NormPayload.Scalars, NormPayload.ScalarMin, NormPayload.ScalarMax);

    AnimationFrames[NormPayload.FrameIndex] = MoveTemp(NormPayload);
    ++ReceivedFrameCount;

    // Pre-build the colormap texture so playback never allocates.
    if (Payload.ColorMapWidth > 0 && Payload.ColorMapHeight > 0 &&
        Payload.ColorMapData.Num() == Payload.ColorMapWidth * Payload.ColorMapHeight * 4)
    {
        FrameTextures[Payload.FrameIndex] = BuildTexture(
            Payload.ColorMapWidth, Payload.ColorMapHeight, Payload.ColorMapData);
    }

    UE_LOG(LogTemp, Log, TEXT("MeshActor: Received frame %d  (%d / %d total)"),
        Payload.FrameIndex, ReceivedFrameCount,
        ExpectedFrameCount > 0 ? ExpectedFrameCount : -1);

    if (ExpectedFrameCount > 0 && ReceivedFrameCount >= ExpectedFrameCount)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("MeshActor: All %d frames received — starting playback at %.1f FPS"),
            ExpectedFrameCount, Payload.PlaybackFPS);
        StartPlayback(Payload.PlaybackFPS > 0.f ? Payload.PlaybackFPS : 24.f);
    }
}

void AMeshActor::StartPlayback(float FPS)
{
    StopPlayback();

    if (AnimationFrames.Num() == 0) return;

    SecondsPerFrame  = FPS > 0.f ? 1.f / FPS : 1.f / 24.f;
    CurrentFrameIdx  = 0;
    FrameAccumulator = 0.f;
    bIsPlaying       = true;

    // Show the first frame immediately.
    const FMeshPayload& First = AnimationFrames[0];
    ProcMesh->ClearMeshSection(0);
    ProcMesh->CreateMeshSection_LinearColor(
        0, First.Vertices, First.Triangles, First.Normals, First.UVs,
        {}, {}, false);
    ApplyFixedBounds();   // override per-frame computed bounds with the global box
    bSectionCreated        = true;
    CurrentVertexCount     = First.Vertices.Num();
    CurrentTriangleCount   = First.Triangles.Num();

    if (FrameTextures.IsValidIndex(0) && FrameTextures[0])
        ApplyTexture(FrameTextures[0]);

    PlaybackHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateUObject(this, &AMeshActor::TickPlayback),
        0.0f   // called every frame
    );
}

void AMeshActor::StopPlayback()
{
    if (bIsPlaying)
    {
        FTSTicker::GetCoreTicker().RemoveTicker(PlaybackHandle);
        bIsPlaying = false;
    }
}

bool AMeshActor::TickPlayback(float DeltaTime)
{
    if (!bIsPlaying || AnimationFrames.Num() == 0) return true;

    FrameAccumulator += DeltaTime;

    while (FrameAccumulator >= SecondsPerFrame)
    {
        FrameAccumulator -= SecondsPerFrame;
        CurrentFrameIdx = (CurrentFrameIdx + 1) % AnimationFrames.Num();

        const FMeshPayload& Frame = AnimationFrames[CurrentFrameIdx];

        // CreateMeshSection_LinearColor resets section data internally, so it
        // handles both topology changes and same-topology attribute updates
        // without needing an explicit ClearMeshSection first.
        ProcMesh->CreateMeshSection_LinearColor(
            0, Frame.Vertices, Frame.Triangles, Frame.Normals, Frame.UVs,
            {}, {}, false);
        ApplyFixedBounds();   // override per-frame computed bounds with the global box
        CurrentVertexCount   = Frame.Vertices.Num();
        CurrentTriangleCount = Frame.Triangles.Num();

        // Swap the pre-built colormap texture.
        if (FrameTextures.IsValidIndex(CurrentFrameIdx) &&
            FrameTextures[CurrentFrameIdx])
        {
            ApplyTexture(FrameTextures[CurrentFrameIdx]);
        }
    }

    return true;
}

// =============================================================================
// Normalization
// =============================================================================

TArray<FVector> AMeshActor::ComputeSmoothNormals(
    const TArray<FVector>& Verts, const TArray<int32>& Tris)
{
    TArray<FVector> Normals;
    Normals.Init(FVector::ZeroVector, Verts.Num());

    // Accumulate area-weighted face normals at each vertex.
    for (int32 i = 0; i + 2 < Tris.Num(); i += 3)
    {
        const int32 i0 = Tris[i], i1 = Tris[i + 1], i2 = Tris[i + 2];
        if (!Verts.IsValidIndex(i0) || !Verts.IsValidIndex(i1) || !Verts.IsValidIndex(i2))
            continue;
        const FVector FaceN = FVector::CrossProduct(
            Verts[i1] - Verts[i0], Verts[i2] - Verts[i0]);
        Normals[i0] += FaceN;
        Normals[i1] += FaceN;
        Normals[i2] += FaceN;
    }

    for (FVector& N : Normals)
        N = N.GetSafeNormal();

    // Outward orientation: flip all normals if they point on average toward
    // the centroid rather than away from it.
    if (Verts.Num() > 0)
    {
        FVector Centroid = FVector::ZeroVector;
        for (const FVector& V : Verts) Centroid += V;
        Centroid /= (float)Verts.Num();

        float DotSum = 0.f;
        for (int32 i = 0; i < Verts.Num(); ++i)
            DotSum += FVector::DotProduct(Verts[i] - Centroid, Normals[i]);

        if (DotSum < 0.f)
            for (FVector& N : Normals) N = -N;
    }

    return Normals;
}

TArray<FVector2D> AMeshActor::ScalarsToUVs(
    const TArray<float>& Scalars, float VMin, float VMax)
{
    TArray<FVector2D> UVs;
    UVs.Reserve(Scalars.Num());
    const float Range = VMax - VMin;
    for (float S : Scalars)
    {
        const float U = Range > 0.f
            ? FMath::Clamp((S - VMin) / Range, 0.f, 1.f)
            : 0.f;
        UVs.Add(FVector2D(U, 0.5f));
    }
    return UVs;
}

void AMeshActor::SetTargetBoxExtents(const FVector& HalfExtents)
{
    if (!TargetHalfExtents.Equals(HalfExtents, 0.01f))
    {
        TargetHalfExtents = HalfExtents;
        bNormLocked = false;   // force re-lock with new box extents
    }
}

void AMeshActor::LockNormalization(const TArray<FVector>& Vertices)
{
    if (Vertices.IsEmpty()) return;

    FVector Min = Vertices[0];
    FVector Max = Vertices[0];
    for (const FVector& V : Vertices)
    {
        Min = Min.ComponentMin(V);
        Max = Max.ComponentMax(V);
    }

    NormCenter = (Min + Max) * 0.5f;

    // Compute a single uniform scale that fits the data's bounding box inside
    // TargetHalfExtents on every axis, preserving the original aspect ratio.
    // S = min_i( 2 * TargetHalfExtents_i / Extents_i )
    // A degenerate axis (zero extent) is skipped so it doesn't drive the scale.
    const FVector Extents = Max - Min;
    float S = FLT_MAX;
    if (Extents.X > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.X / Extents.X);
    if (Extents.Y > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.Y / Extents.Y);
    if (Extents.Z > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.Z / Extents.Z);
    NormScale = (S < FLT_MAX) ? S : 1.f;

    bNormLocked     = true;
    bHasFixedBounds = false;   // no global bounds → can't lock the component AABB
}

void AMeshActor::LockNormalizationFromBounds(const FVector& BoundsMin, const FVector& BoundsMax)
{
    NormCenter = (BoundsMin + BoundsMax) * 0.5f;

    // Compute a single uniform scale that fits the ParaView bounding box inside
    // TargetHalfExtents on every axis, preserving the original aspect ratio.
    // S = min_i( 2 * TargetHalfExtents_i / PVExtents_i )
    const FVector PVExtents = BoundsMax - BoundsMin;
    float S = FLT_MAX;
    if (PVExtents.X > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.X / PVExtents.X);
    if (PVExtents.Y > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.Y / PVExtents.Y);
    if (PVExtents.Z > 0.f) S = FMath::Min(S, 2.f * TargetHalfExtents.Z / PVExtents.Z);
    NormScale = (S < FLT_MAX) ? S : 1.f;

    bNormLocked = true;

    // Pre-compute the post-normalization local bounding box.
    // All frames' vertices fit inside this box, so we can lock ProcMesh->Bounds
    // to it after every CreateMeshSection_LinearColor call and prevent the
    // engine from recomputing per-frame bounds from actual vertex positions.
    FixedBoundsMin  = (BoundsMin - NormCenter) * NormScale;
    FixedBoundsMax  = (BoundsMax - NormCenter) * NormScale;
    bHasFixedBounds = true;
}

void AMeshActor::ApplyFixedBounds() const
{
    if (!bHasFixedBounds) return;

    // UProceduralMeshComponent::CreateMeshSection_LinearColor calls
    // UpdateLocalBounds() → UpdateBounds() which overwrites ProcMesh->Bounds
    // with the per-frame AABB.  We immediately replace it with the fixed global
    // box so the bounding box stays stable throughout the animation.
    const FBox LocalBox(FixedBoundsMin, FixedBoundsMax);
    ProcMesh->Bounds = FBoxSphereBounds(LocalBox)
        .TransformBy(ProcMesh->GetComponentTransform());
}

void AMeshActor::NormalizeVertices(TArray<FVector>& Vertices) const
{
    for (FVector& V : Vertices)
        V = (V - NormCenter) * NormScale;
}

// =============================================================================
// Helpers
// =============================================================================

UTexture2D* AMeshActor::BuildTexture(int32 Width, int32 Height,
                                      const TArray<uint8>& Data) const
{
    UTexture2D* Tex = UTexture2D::CreateTransient(Width, Height, PF_R8G8B8A8);
    Tex->Filter   = TF_Bilinear;
    Tex->AddressX = TA_Clamp;
    Tex->AddressY = TA_Clamp;

    FTexture2DMipMap& Mip = Tex->GetPlatformData()->Mips[0];
    uint8* Ptr = static_cast<uint8*>(Mip.BulkData.Lock(LOCK_READ_WRITE));
    FMemory::Memcpy(Ptr, Data.GetData(), Data.Num());
    Mip.BulkData.Unlock();
    Tex->UpdateResource();

    return Tex;
}

void AMeshActor::ApplyTexture(UTexture2D* Texture)
{
    ColorMapTexture = Texture;

    if (!DynamicMaterial)
    {
        UMaterialInterface* Base = ProcMesh->GetMaterial(0);
        DynamicMaterial = UMaterialInstanceDynamic::Create(Base, this);
        ProcMesh->SetMaterial(0, DynamicMaterial);
    }

    DynamicMaterial->SetTextureParameterValue(TEXT("ColorMap"), ColorMapTexture);
}
