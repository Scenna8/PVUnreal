#include "MeshSocketSubsystem.h"
#include "Networking.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Async/Async.h"

// =============================================================================
// FSocketListenerThread
// =============================================================================

FSocketListenerThread::FSocketListenerThread(
    TQueue<FMeshPayload, EQueueMode::Mpsc>* InQueue, int32 InPort)
    : Queue(InQueue), Port(InPort)
{
}

bool FSocketListenerThread::Init()
{
    FIPv4Address Addr(127, 0, 0, 1);
    FIPv4Endpoint Endpoint(Addr, Port);

    ListenSocket = FTcpSocketBuilder(TEXT("MeshListener"))
        .AsReusable()
        .BoundToEndpoint(Endpoint)
        .Listening(8);   // backlog of 8 so Python can queue connections

    if (!ListenSocket)
    {
        UE_LOG(LogTemp, Error, TEXT("MeshSocket: Failed to create listen socket on port %d"), Port);
        return false;
    }

    UE_LOG(LogTemp, Warning, TEXT("MeshSocket: Listening on port %d"), Port);
    return true;
}

// Static — safe to call from any thread (no instance state).
bool FSocketListenerThread::RecvAll(FSocket* Socket, uint8* Buffer, int32 NumBytes)
{
    int32 BytesRead = 0;
    while (BytesRead < NumBytes)
    {
        if (!Socket->Wait(ESocketWaitConditions::WaitForRead, FTimespan::FromSeconds(30)))
            return false;   // timed out or connection closed

        int32 Read = 0;
        Socket->Recv(Buffer + BytesRead, NumBytes - BytesRead, Read);
        if (Read <= 0)
            return false;

        BytesRead += Read;
    }
    return true;
}

uint32 FSocketListenerThread::Run()
{
    while (bRunning)
    {
        bool bHasPending = false;
        ListenSocket->WaitForPendingConnection(bHasPending, FTimespan::FromSeconds(1));
        if (!bHasPending) continue;

        TSharedRef<FInternetAddr> RemoteAddr =
            ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->CreateInternetAddr();
        FSocket* Client = ListenSocket->Accept(*RemoteAddr, TEXT("MeshClient"));
        if (!Client) continue;

        // Hand the accepted socket to a thread-pool task immediately so the
        // accept loop is free to receive the next frame right away.
        TQueue<FMeshPayload, EQueueMode::Mpsc>* Q = Queue;
        Async(EAsyncExecution::ThreadPool, [Client, Q]()
        {
            // ---- 4-byte big-endian length header ----
            uint8 Header[4];
            if (!RecvAll(Client, Header, 4))
            {
                UE_LOG(LogTemp, Warning, TEXT("MeshSocket: Failed to read header, dropping"));
                Client->Close();
                ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Client);
                return;
            }

            const uint32 MsgLen =
                (uint32(Header[0]) << 24) |
                (uint32(Header[1]) << 16) |
                (uint32(Header[2]) <<  8) |
                 uint32(Header[3]);

            // ---- JSON payload ----
            TArray<uint8> Buffer;
            Buffer.SetNumUninitialized(MsgLen + 1);   // +1 for null terminator

            if (!RecvAll(Client, Buffer.GetData(), MsgLen))
            {
                UE_LOG(LogTemp, Warning, TEXT("MeshSocket: Incomplete payload, dropping"));
                Client->Close();
                ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Client);
                return;
            }
            Buffer[MsgLen] = 0;

            Client->Close();
            ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Client);

            FString JsonStr = FString(UTF8_TO_TCHAR(
                reinterpret_cast<const char*>(Buffer.GetData())));

            // ---- Parse ----
            TSharedPtr<FJsonObject> JsonObj;
            TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonStr);
            if (!FJsonSerializer::Deserialize(Reader, JsonObj))
            {
                UE_LOG(LogTemp, Error, TEXT("MeshSocket: JSON parse failed"));
                return;
            }

            // ---- Fill payload ----
            FMeshPayload Payload;

            if (JsonObj->HasField(TEXT("id")))
                Payload.MeshId = JsonObj->GetStringField(TEXT("id"));

            if (JsonObj->HasField(TEXT("volume_id")))
                Payload.VolumeId = (int32)JsonObj->GetNumberField(TEXT("volume_id"));

            if (JsonObj->HasField(TEXT("frame")))
                Payload.FrameIndex = (int32)JsonObj->GetNumberField(TEXT("frame"));
            if (JsonObj->HasField(TEXT("total_frames")))
                Payload.TotalFrames = (int32)JsonObj->GetNumberField(TEXT("total_frames"));
            if (JsonObj->HasField(TEXT("fps")))
                Payload.PlaybackFPS = (float)JsonObj->GetNumberField(TEXT("fps"));

            for (auto& V : JsonObj->GetArrayField(TEXT("verts")))
            {
                auto Obj = V->AsObject();
                Payload.Vertices.Add(FVector(
                    Obj->GetNumberField(TEXT("X")),
                    Obj->GetNumberField(TEXT("Y")),
                    Obj->GetNumberField(TEXT("Z"))));
            }

            for (auto& T : JsonObj->GetArrayField(TEXT("tris")))
                Payload.Triangles.Add((int32)T->AsNumber());

            // Scalars and their range — Unreal maps these to UVs and computes normals.
            if (JsonObj->HasField(TEXT("scalars")))
            {
                for (auto& S : JsonObj->GetArrayField(TEXT("scalars")))
                    Payload.Scalars.Add((float)S->AsNumber());
            }
            if (JsonObj->HasField(TEXT("scalar_min")))
                Payload.ScalarMin = (float)JsonObj->GetNumberField(TEXT("scalar_min"));
            if (JsonObj->HasField(TEXT("scalar_max")))
                Payload.ScalarMax = (float)JsonObj->GetNumberField(TEXT("scalar_max"));

            if (JsonObj->HasField(TEXT("colormap")))
            {
                auto CmObj = JsonObj->GetObjectField(TEXT("colormap"));
                Payload.ColorMapWidth  = (int32)CmObj->GetNumberField(TEXT("w"));
                Payload.ColorMapHeight = (int32)CmObj->GetNumberField(TEXT("h"));
                for (auto& Px : CmObj->GetArrayField(TEXT("data")))
                    Payload.ColorMapData.Add((uint8)Px->AsNumber());
            }

            UE_LOG(LogTemp, Log, TEXT("MeshSocket: Parsed frame %d — %d verts, %d tris"),
                Payload.FrameIndex, Payload.Vertices.Num(), Payload.Triangles.Num());

            Q->Enqueue(MoveTemp(Payload));
        });
    }

    return 0;
}

void FSocketListenerThread::Stop()
{
    bRunning = false;
}

// =============================================================================
// UMeshSocketSubsystem
// =============================================================================

bool UMeshSocketSubsystem::ShouldCreateSubsystem(UObject* Outer) const
{
    TArray<UClass*> DerivedClasses;
    GetDerivedClasses(GetClass(), DerivedClasses, true);
    return DerivedClasses.Num() == 0;
}

void UMeshSocketSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);

    ListenerThread = new FSocketListenerThread(&PayloadQueue, 9000);
    RunnableThread = FRunnableThread::Create(ListenerThread, TEXT("MeshListenerThread"));

    TickerHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateUObject(this, &UMeshSocketSubsystem::DrainQueue),
        1.0f / 30.0f
    );

    UE_LOG(LogTemp, Warning, TEXT("MeshSocketSubsystem: Initialized."));
}

void UMeshSocketSubsystem::Deinitialize()
{
    FTSTicker::GetCoreTicker().RemoveTicker(TickerHandle);

    if (ListenerThread) { ListenerThread->Stop(); }
    if (RunnableThread)
    {
        RunnableThread->Kill(true);
        delete RunnableThread;
        RunnableThread = nullptr;
    }
    if (ListenerThread)
    {
        delete ListenerThread;
        ListenerThread = nullptr;
    }

    UE_LOG(LogTemp, Warning, TEXT("MeshSocketSubsystem: Deinitialized."));

    Super::Deinitialize();
}

bool UMeshSocketSubsystem::DrainQueue(float DeltaTime)
{
    // One item per tick — the game thread consumes frames at roughly the
    // same pace they arrive from the sender rather than processing a whole
    // burst at once.
    FMeshPayload Payload;
    if (PayloadQueue.Dequeue(Payload))
    {
        HandlePayload(Payload);
    }
    return true;
}

void UMeshSocketSubsystem::HandlePayload(const FMeshPayload& Payload)
{
    // Default behaviour: print to Output Log so you can verify the sender
    // without needing a mesh in the scene.
    UE_LOG(LogTemp, Warning, TEXT("=== MeshSocket: Payload received ==="));
    UE_LOG(LogTemp, Warning, TEXT("  Verts: %d"), Payload.Vertices.Num());

    for (int32 i = 0; i < Payload.Vertices.Num(); i++)
    {
        UE_LOG(LogTemp, Log, TEXT("    [%d] X=%.2f  Y=%.2f  Z=%.2f"),
            i,
            Payload.Vertices[i].X,
            Payload.Vertices[i].Y,
            Payload.Vertices[i].Z);
    }

    UE_LOG(LogTemp, Warning, TEXT("  Tris: %d indices (%d triangles)"),
        Payload.Triangles.Num(),
        Payload.Triangles.Num() / 3);

    for (int32 i = 0; i + 2 < Payload.Triangles.Num(); i += 3)
    {
        UE_LOG(LogTemp, Log, TEXT("    [%d] %d, %d, %d"),
            i / 3,
            Payload.Triangles[i],
            Payload.Triangles[i + 1],
            Payload.Triangles[i + 2]);
    }

    UE_LOG(LogTemp, Warning, TEXT("===================================="));
}
