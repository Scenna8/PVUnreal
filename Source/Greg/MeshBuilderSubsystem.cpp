#include "MeshBuilderSubsystem.h"
#include "MeshActor.h"
#include "OrbitCameraActor.h"
#include "DisplayVolumeActor.h"
#include "Components/BoxComponent.h"
#include "Kismet/GameplayStatics.h"
#include "GameFramework/PlayerController.h"
#include "Networking.h"

// =============================================================================
// FVolumeQueryServer
//
// Listens on port 9001.  For every incoming connection it sends the
// alphabetically-sorted list of ADisplayVolumeActor VolumeName strings:
//
//   [4-byte big-endian count N]
//   for each name:
//     [4-byte big-endian UTF-8 byte length]
//     [UTF-8 bytes]
//
// The ParaView plugin shows these names in a dropdown.  The selected name's
// 0-based position in the list is sent back as the integer VolumeId — users
// never see the number.
//
// The name list is refreshed every second on the game thread and guarded by
// a critical section so this thread never touches UWorld directly.
// =============================================================================

class FVolumeQueryServer : public FRunnable
{
public:
    FVolumeQueryServer(TArray<FString>* InNames, FCriticalSection* InLock, int32 InPort)
        : NamesPtr(InNames), LockPtr(InLock), Port(InPort) {}

    bool Init() override
    {
        FIPv4Address Addr(127, 0, 0, 1);
        FIPv4Endpoint Endpoint(Addr, Port);

        ListenSocket = FTcpSocketBuilder(TEXT("VolumeQueryListener"))
            .AsReusable()
            .BoundToEndpoint(Endpoint)
            .Listening(4);

        if (!ListenSocket)
        {
            UE_LOG(LogTemp, Error,
                TEXT("VolumeQuery: Failed to bind on port %d"), Port);
            return false;
        }
        UE_LOG(LogTemp, Warning, TEXT("VolumeQuery: Listening on port %d"), Port);
        return true;
    }

    uint32 Run() override
    {
        while (bRunning)
        {
            bool bPending = false;
            ListenSocket->WaitForPendingConnection(bPending, FTimespan::FromSeconds(1));
            if (!bPending) continue;

            TSharedRef<FInternetAddr> RemoteAddr =
                ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->CreateInternetAddr();
            FSocket* Client = ListenSocket->Accept(*RemoteAddr, TEXT("VolumeQueryClient"));
            if (!Client) continue;

            // Snapshot the name list under the lock.
            TArray<FString> Snapshot;
            {
                FScopeLock Lock(LockPtr);
                Snapshot = *NamesPtr;
            }

            // Build the response buffer.
            TArray<uint8> Buf;
            const int32 N = Snapshot.Num();

            // 4-byte count
            Buf.Add(uint8((N >> 24) & 0xFF));
            Buf.Add(uint8((N >> 16) & 0xFF));
            Buf.Add(uint8((N >>  8) & 0xFF));
            Buf.Add(uint8( N        & 0xFF));

            for (const FString& Name : Snapshot)
            {
                // Convert to UTF-8
                FTCHARToUTF8 UTF8(*Name, Name.Len());
                const int32 Len = UTF8.Length();

                // 4-byte length
                Buf.Add(uint8((Len >> 24) & 0xFF));
                Buf.Add(uint8((Len >> 16) & 0xFF));
                Buf.Add(uint8((Len >>  8) & 0xFF));
                Buf.Add(uint8( Len        & 0xFF));

                // UTF-8 bytes
                for (int32 i = 0; i < Len; ++i)
                    Buf.Add((uint8)UTF8.Get()[i]);
            }

            int32 Sent = 0;
            Client->Send(Buf.GetData(), Buf.Num(), Sent);
            Client->Close();
            ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Client);
        }

        if (ListenSocket)
        {
            ListenSocket->Close();
            ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ListenSocket);
            ListenSocket = nullptr;
        }
        return 0;
    }

    void Stop() override { bRunning = false; }

private:
    TArray<FString>*  NamesPtr     = nullptr;
    FCriticalSection* LockPtr      = nullptr;
    FSocket*          ListenSocket = nullptr;
    int32             Port         = 9001;
    bool              bRunning     = true;
};

// =============================================================================
// Lifecycle
// =============================================================================

void UMeshBuilderSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);  // starts socket thread + DrainQueue ticker

    // Refresh the cached volume name list every second on the game thread.
    VolumeCountTickHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateUObject(this, &UMeshBuilderSubsystem::RefreshVolumeList),
        1.0f);

    // Start the query server so ParaView can fetch the list of volume names.
    VolumeQueryServer = new FVolumeQueryServer(
        &CachedVolumeNames, &CachedVolumeNamesLock, 9001);
    VolumeQueryThread = FRunnableThread::Create(
        VolumeQueryServer, TEXT("VolumeQueryThread"));
}

void UMeshBuilderSubsystem::Deinitialize()
{
    FTSTicker::GetCoreTicker().RemoveTicker(VolumeCountTickHandle);

    if (VolumeQueryServer) { VolumeQueryServer->Stop(); }
    if (VolumeQueryThread)
    {
        VolumeQueryThread->Kill(true);
        delete VolumeQueryThread;
        VolumeQueryThread = nullptr;
    }
    delete VolumeQueryServer;
    VolumeQueryServer = nullptr;

    Super::Deinitialize();
}

bool UMeshBuilderSubsystem::RefreshVolumeList(float /*DeltaTime*/)
{
    UWorld* World = GetGameInstance() ? GetGameInstance()->GetWorld() : nullptr;
    if (!World) return true;

    const TArray<ADisplayVolumeActor*> Sorted = GetSortedVolumeActors(World);

    // Build a sorted list of names (unique) and a set for fast membership tests.
    TSet<FString>  LiveNames;
    TArray<FString> SortedNames;
    for (ADisplayVolumeActor* V : Sorted)
    {
        LiveNames.Add(V->VolumeName);
        SortedNames.AddUnique(V->VolumeName);
    }

    // Publish to the query server thread under the lock.
    {
        FScopeLock Lock(&CachedVolumeNamesLock);
        CachedVolumeNames = SortedNames;
    }

    // Destroy any mesh actors whose DisplayVolumeActor has been removed.
    TArray<FString> OrphanedKeys;
    for (auto& Pair : MeshActors)
    {
        const FString* NamePtr = MeshActorVolumeNames.Find(Pair.Key);
        if (!NamePtr || !LiveNames.Contains(*NamePtr))
            OrphanedKeys.Add(Pair.Key);
    }

    for (const FString& Key : OrphanedKeys)
    {
        if (AMeshActor* Mesh = MeshActors.FindRef(Key))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("MeshBuilder: Volume removed — destroying orphaned mesh '%s'"), *Key);
            Mesh->Destroy();
        }
        MeshActors.Remove(Key);
        MeshActorVolumeNames.Remove(Key);
    }

    return true;
}

// =============================================================================
// Payload handling
// =============================================================================

void UMeshBuilderSubsystem::HandlePayload(const FMeshPayload& Payload)
{
    UWorld* World = GetGameInstance()->GetWorld();
    if (!World)
    {
        UE_LOG(LogTemp, Warning, TEXT("MeshBuilder: No world available, dropping payload"));
        return;
    }

    const FString& Id           = Payload.MeshId;
    const int32    VolumeIndex  = Payload.VolumeId;   // 0-based index into sorted names

    // Resolve index → name → transforms in one sorted-actor pass.
    const TArray<ADisplayVolumeActor*> Sorted = GetSortedVolumeActors(World);

    FString            VolumeName;
    TArray<FTransform> Transforms;
    TArray<FVector>    HalfExtents;   // parallel to Transforms

    if (Sorted.IsValidIndex(VolumeIndex))
    {
        VolumeName = Sorted[VolumeIndex]->VolumeName;

        // All actors sharing that name receive the mesh simultaneously.
        for (ADisplayVolumeActor* V : Sorted)
        {
            if (V->VolumeName != VolumeName) continue;
            const FVector HalfExtent = V->VolumeBox->GetScaledBoxExtent();
            UE_LOG(LogTemp, Log,
                TEXT("MeshBuilder: Matched '%s' at %s, box half-extents %s"),
                *VolumeName, *V->GetActorLocation().ToString(), *HalfExtent.ToString());
            // Scale = 1: MeshActor derives a uniform aspect-ratio-preserving scale
            // from the ParaView BB → this box, so the actor transform only carries
            // position and orientation.
            Transforms.Add(FTransform(V->GetActorQuat(), V->GetActorLocation(), FVector::OneVector));
            HalfExtents.Add(HalfExtent);
        }
    }

    if (Transforms.IsEmpty())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("MeshBuilder: No DisplayVolumeActor at index %d — using origin"), VolumeIndex);
        VolumeName = TEXT("(origin)");
        Transforms.Add(FTransform::Identity);
        HalfExtents.Add(FVector(100.f));   // default: 100 cm cube
    }

    const bool bFirstEver = MeshActors.IsEmpty();

    for (int32 i = 0; i < Transforms.Num(); i++)
    {
        // Single match → key is just MeshId.  Multiple matches → append "#0", "#1", …
        const FString Key = (Transforms.Num() == 1)
            ? Id
            : FString::Printf(TEXT("%s#%d"), *Id, i);

        AMeshActor* Mesh = nullptr;

        if (!MeshActors.Contains(Key))
        {
            FActorSpawnParameters Params;
            Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;

            Mesh = World->SpawnActor<AMeshActor>(
                AMeshActor::StaticClass(), Transforms[i], Params);
            MeshActors.Add(Key, Mesh);
            MeshActorVolumeNames.Add(Key, VolumeName);

            UE_LOG(LogTemp, Warning,
                TEXT("MeshBuilder: Spawned '%s' → '%s'  (%d actor(s) total)"),
                *Key, *VolumeName, MeshActors.Num());
        }
        else
        {
            Mesh = MeshActors[Key];
            Mesh->SetActorTransform(Transforms[i]);
        }

        if (Mesh)
        {
            // Tell MeshActor the UE box extents so it can fit the ParaView BB
            // into this box with a uniform scale (aspect ratio preserved).
            Mesh->SetTargetBoxExtents(HalfExtents[i]);

            if (Payload.FrameIndex >= 0)
                Mesh->AddAnimationFrame(Payload);
            else
                Mesh->ApplyMesh(Payload);
        }
    }

    // Log geometry stats once (not per instance).
    if (Payload.FrameIndex < 0)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("MeshBuilder: Updated '%s' → '%s'  %d instance(s), %d verts, %d tris"),
            *Id, *VolumeName, Transforms.Num(),
            Payload.Vertices.Num(), Payload.Triangles.Num());
    }
    else
    {
        UE_LOG(LogTemp, Log,
            TEXT("MeshBuilder: Buffered frame %d for '%s' → '%s'  %d instance(s)"),
            Payload.FrameIndex, *Id, *VolumeName, Transforms.Num());
    }

    // Spawn orbit camera the first time any mesh arrives.
    if (bFirstEver && !MeshActors.IsEmpty())
    {
        APlayerController* PC = World->GetFirstPlayerController();
        FTransform CamTransform(Transforms[0].GetRotation(), Transforms[0].GetLocation());
        OrbitCamera = World->SpawnActor<AOrbitCameraActor>(
            AOrbitCameraActor::StaticClass(), CamTransform);
        if (OrbitCamera && PC)
        {
            PC->SetViewTargetWithBlend(OrbitCamera, 0.5f);
            PC->bShowMouseCursor = true;
        }
    }
}

// =============================================================================
// Helpers
// =============================================================================

TArray<ADisplayVolumeActor*> UMeshBuilderSubsystem::GetSortedVolumeActors(UWorld* World)
{
    TArray<AActor*> All;
    UGameplayStatics::GetAllActorsOfClass(
        World, ADisplayVolumeActor::StaticClass(), All);

    TArray<ADisplayVolumeActor*> Sorted;
    for (AActor* A : All)
        if (ADisplayVolumeActor* V = Cast<ADisplayVolumeActor>(A))
            Sorted.Add(V);

    Sorted.Sort([](const ADisplayVolumeActor& A, const ADisplayVolumeActor& B)
    {
        return A.VolumeName < B.VolumeName;
    });

    return Sorted;
}

TArray<FTransform> UMeshBuilderSubsystem::GetDisplayVolumeTransforms(
    UWorld* World, int32 VolumeIndex)
{
    const TArray<ADisplayVolumeActor*> Sorted = GetSortedVolumeActors(World);
    if (!Sorted.IsValidIndex(VolumeIndex)) return {};

    const FString TargetName = Sorted[VolumeIndex]->VolumeName;

    TArray<FTransform> Result;
    for (ADisplayVolumeActor* V : Sorted)
    {
        if (V->VolumeName != TargetName) continue;
        // Scale = 1: MeshActor owns the uniform aspect-ratio-preserving scale.
        Result.Add(FTransform(V->GetActorQuat(), V->GetActorLocation(), FVector::OneVector));
    }
    return Result;
}
