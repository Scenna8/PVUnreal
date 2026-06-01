#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "Components/BoxComponent.h"
#include "DisplayVolumeActor.generated.h"

/**
 * ADisplayVolumeActor
 *
 * Place one of these in the level to define a named world-space region where
 * incoming mesh data will be displayed.
 *
 * Give each actor a unique VolumeName in the Details panel.  The ParaView
 * plugin queries Unreal for the list of names and shows them in a dropdown —
 * the user picks by name and never sees an integer.  Internally Unreal routes
 * the mesh by the selected name's position in the alphabetically-sorted list.
 *
 * Scale mapping:
 *   Unreal normalises incoming geometry to ±100 cm, then sets the MeshActor
 *   scale so that coordinate space maps exactly onto this box:
 *
 *       MeshActor.Scale = VolumeBox.GetScaledBoxExtent() / 100
 *
 *   Resize the box in the editor to stretch or compress data into any region.
 *   The box outline is hidden during PIE but visible in the editor viewport.
 */
UCLASS()
class GREG_API ADisplayVolumeActor : public AActor
{
    GENERATED_BODY()

public:
    ADisplayVolumeActor();

    /** The box that defines the display volume in world space. */
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Display Volume")
    UBoxComponent* VolumeBox;

    /**
     * Human-readable name shown in the ParaView "Display Volume" dropdown.
     * Names are sorted alphabetically before being sent to ParaView.
     * Freshly placed actors are auto-named "Volume N" (lowest unused N).
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Display Volume")
    FString VolumeName = TEXT("Volume 1");

#if WITH_EDITOR
    /**
     * Auto-assigns a name of the form "Volume N" (lowest unused N ≥ 1) when
     * the actor is freshly placed or duplicated.  Never called on load, so
     * saved names are always preserved.
     */
    virtual void PostActorCreated() override;
#endif
};
