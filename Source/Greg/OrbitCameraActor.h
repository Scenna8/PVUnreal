#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "OrbitCameraActor.generated.h"

class USpringArmComponent;
class UCameraComponent;

/**
 * AOrbitCameraActor
 *
 * A camera that sits at a fixed world position and orbits around it in
 * response to left-mouse-button drag. Spawned by UMeshBuilderSubsystem
 * when the first mesh payload arrives; the player controller's view is
 * then blended to this camera.
 *
 * Orbit controls:
 *   Left mouse button + drag  — rotate horizontally and vertically
 *   (Scroll-to-zoom can be added later via ArmLength adjustment)
 */
UCLASS()
class GREG_API AOrbitCameraActor : public AActor
{
    GENERATED_BODY()

public:
    AOrbitCameraActor();
    virtual void Tick(float DeltaTime) override;

    /**
     * Teleport the pivot to MeshLocation and reset the orbit angles to their
     * defaults. Called by MeshBuilderSubsystem when the active mesh changes.
     */
    void ResetToMesh(FVector MeshLocation);

    /** Degrees of rotation per pixel of mouse movement. */
    UPROPERTY(EditAnywhere, Category="Orbit")
    float OrbitSpeed = 0.5f;

    /** Distance from the pivot to the camera (cm). */
    UPROPERTY(EditAnywhere, Category="Orbit")
    float ArmLength = 500.f;

private:
    /** The pivot sits at the mesh centre — rotating this orbits the camera. */
    UPROPERTY(VisibleAnywhere)
    USceneComponent* Pivot;

    UPROPERTY(VisibleAnywhere)
    USpringArmComponent* SpringArm;

    UPROPERTY(VisibleAnywhere)
    UCameraComponent* Camera;

    float CurrentYaw   =   0.f;
    float CurrentPitch = -20.f;   // slight top-down starting angle

    // Used to compute per-frame mouse delta when the cursor is visible.
    float LastMouseX   =   0.f;
    float LastMouseY   =   0.f;
    bool  bWasDragging = false;
};
