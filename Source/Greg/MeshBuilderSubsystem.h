#pragma once

#include "CoreMinimal.h"
#include "MeshSocketSubsystem.h"
#include "Containers/Ticker.h"
#include "HAL/CriticalSection.h"
#include "MeshBuilderSubsystem.generated.h"

class AMeshActor;
class AOrbitCameraActor;
class ADisplayVolumeActor;
class FVolumeQueryServer;

/**
 * UMeshBuilderSubsystem
 *
 * Manages named mesh actors in the world.  Each payload carries a MeshId and
 * a VolumeIndex (0-based index into the alphabetically-sorted list of
 * ADisplayVolumeActors by VolumeName).
 *
 * Port 9001 query server:
 *   Sends the sorted list of VolumeName strings so ParaView can show a
 *   human-readable dropdown.  The selected name's array index is what gets
 *   transmitted back as the integer VolumeId — users never see the number.
 */
UCLASS()
class GREG_API UMeshBuilderSubsystem : public UMeshSocketSubsystem
{
    GENERATED_BODY()

public:
    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

protected:
    virtual void HandlePayload(const FMeshPayload& Payload) override;

private:
    // ---- Mesh actors -------------------------------------------------------

    UPROPERTY()
    TMap<FString, AMeshActor*> MeshActors;

    /** Maps each MeshActors key to the VolumeName it was placed in.
     *  Used by RefreshVolumeList to destroy actors whose volume was deleted. */
    TMap<FString, FString> MeshActorVolumeNames;

    // ---- Camera ------------------------------------------------------------

    UPROPERTY()
    AOrbitCameraActor* OrbitCamera = nullptr;

    // ---- Volume query server -----------------------------------------------

    /** Alphabetically-sorted list of VolumeNames currently in the world.
     *  Written on the game thread, read by the query server thread. */
    TArray<FString>  CachedVolumeNames;
    FCriticalSection CachedVolumeNamesLock;

    /** Ticker that refreshes CachedVolumeNames and removes orphaned actors. */
    FTSTicker::FDelegateHandle VolumeCountTickHandle;

    /** Background thread that serves the name list on port 9001. */
    FVolumeQueryServer* VolumeQueryServer = nullptr;
    FRunnableThread*    VolumeQueryThread  = nullptr;

    /** Game-thread ticker: rebuild the sorted name list, clean up orphans. */
    bool RefreshVolumeList(float DeltaTime);

    // ---- Helpers -----------------------------------------------------------

    /** Returns all ADisplayVolumeActors sorted alphabetically by VolumeName. */
    static TArray<ADisplayVolumeActor*> GetSortedVolumeActors(UWorld* World);

    /**
     * Returns one FTransform per ADisplayVolumeActor whose VolumeName matches
     * the actor at VolumeIndex in the sorted list.
     * Multiple actors sharing the same name all receive the mesh.
     * Returns an empty array when VolumeIndex is out of range.
     */
    static TArray<FTransform> GetDisplayVolumeTransforms(UWorld* World, int32 VolumeIndex);
};
