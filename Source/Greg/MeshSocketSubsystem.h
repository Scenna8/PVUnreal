#pragma once

#include "CoreMinimal.h"
#include "Subsystems/GameInstanceSubsystem.h"
#include "HAL/Runnable.h"
#include "Containers/Ticker.h"
#include "MeshPayload.h"
#include "MeshSocketSubsystem.generated.h"

// =============================================================================
// FSocketListenerThread
// =============================================================================

/**
 * Background thread that owns the TCP listen socket.
 * Accepts connections, reads length-prefixed JSON payloads, parses them into
 * FMeshPayload structs, and pushes them into a lock-free queue.
 *
 * This class knows nothing about what happens to the data after enqueuing.
 */
class FSocketListenerThread : public FRunnable
{
public:
    FSocketListenerThread(TQueue<FMeshPayload, EQueueMode::Mpsc>* InQueue, int32 InPort);

    virtual bool   Init() override;
    virtual uint32 Run()  override;
    virtual void   Stop() override;

private:
    TQueue<FMeshPayload, EQueueMode::Mpsc>* Queue;

    FSocket* ListenSocket = nullptr;

    int32 Port;
    bool  bRunning = true;

    // Reads exactly NumBytes from Socket into Buffer.
    // Returns false if the connection closes or times out.
    // Static so it can be called from async handler lambdas.
    static bool RecvAll(FSocket* Socket, uint8* Buffer, int32 NumBytes);
};

// =============================================================================
// UMeshSocketSubsystem
// =============================================================================

/**
 * UMeshSocketSubsystem — the base class of the mesh pipeline.
 *
 * Handles all TCP communication: owns the background listener thread, the
 * lock-free queue, and the game-thread ticker that drains it.
 *
 * Default behaviour (used when no subclass exists):
 *   Prints each received payload to the Output Log — vertex positions and
 *   triangle indices — so you can verify the sender without needing a mesh.
 *
 * To change what happens with received data, subclass this and override
 * HandlePayload(). The child class inherits the full I/O stack for free.
 *
 * ShouldCreateSubsystem() is overridden so that if a subclass exists in the
 * project, only the subclass is instantiated (not both parent and child).
 */
UCLASS()
class GREG_API UMeshSocketSubsystem : public UGameInstanceSubsystem
{
    GENERATED_BODY()

public:
    /**
     * Steps aside if a more-derived subclass exists in the project, so that
     * Unreal doesn't instantiate both the parent and the child simultaneously.
     */
    virtual bool ShouldCreateSubsystem(UObject* Outer) const override;

    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

protected:
    /**
     * Called on the game thread each time a complete payload arrives.
     * Override this in subclasses to change what happens with the data.
     *
     * Default implementation: logs vertex positions and triangle indices
     * to the Output Log.
     */
    virtual void HandlePayload(const FMeshPayload& Payload);

private:
    // Drains one item from the queue and calls HandlePayload().
    // Registered as a core ticker; fires ~30x per second.
    bool DrainQueue(float DeltaTime);

    TQueue<FMeshPayload, EQueueMode::Mpsc> PayloadQueue;

    FSocketListenerThread* ListenerThread = nullptr;
    FRunnableThread*       RunnableThread = nullptr;

    FTSTicker::FDelegateHandle TickerHandle;
};
