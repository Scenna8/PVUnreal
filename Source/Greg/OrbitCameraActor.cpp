#include "OrbitCameraActor.h"
#include "GameFramework/SpringArmComponent.h"
#include "Camera/CameraComponent.h"
#include "GameFramework/PlayerController.h"

AOrbitCameraActor::AOrbitCameraActor()
{
    PrimaryActorTick.bCanEverTick = true;

    Pivot = CreateDefaultSubobject<USceneComponent>(TEXT("Pivot"));
    RootComponent = Pivot;

    SpringArm = CreateDefaultSubobject<USpringArmComponent>(TEXT("SpringArm"));
    SpringArm->SetupAttachment(Pivot);
    SpringArm->TargetArmLength = ArmLength;
    SpringArm->bDoCollisionTest         = false;
    SpringArm->bUsePawnControlRotation  = false;
    SpringArm->bInheritPitch            = false;
    SpringArm->bInheritYaw              = false;
    SpringArm->bInheritRoll             = false;

    Camera = CreateDefaultSubobject<UCameraComponent>(TEXT("Camera"));
    Camera->SetupAttachment(SpringArm, USpringArmComponent::SocketName);

    // Start at the initial pitch/yaw
    SpringArm->SetRelativeRotation(FRotator(CurrentPitch, CurrentYaw, 0.f));
}

void AOrbitCameraActor::ResetToMesh(FVector MeshLocation)
{
    SetActorLocation(MeshLocation);
    CurrentYaw   =   0.f;
    CurrentPitch = -20.f;
    SpringArm->SetRelativeRotation(FRotator(CurrentPitch, CurrentYaw, 0.f));
}

void AOrbitCameraActor::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    APlayerController* PC = GetWorld()->GetFirstPlayerController();
    if (!PC) return;

    float MouseX, MouseY;
    PC->GetMousePosition(MouseX, MouseY);

    if (PC->IsInputKeyDown(EKeys::LeftMouseButton))
    {
        // Skip the first frame of a new drag to avoid a position jump.
        if (bWasDragging)
        {
            const float DeltaX = MouseX - LastMouseX;
            const float DeltaY = MouseY - LastMouseY;

            CurrentYaw   += DeltaX * OrbitSpeed;
            CurrentPitch  = FMath::Clamp(CurrentPitch - DeltaY * OrbitSpeed, -80.f, 10.f);

            SpringArm->SetRelativeRotation(FRotator(CurrentPitch, CurrentYaw, 0.f));
        }
        bWasDragging = true;
    }
    else
    {
        bWasDragging = false;
    }

    LastMouseX = MouseX;
    LastMouseY = MouseY;
}
