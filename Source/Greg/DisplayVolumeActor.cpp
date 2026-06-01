#include "DisplayVolumeActor.h"
#include "Kismet/GameplayStatics.h"

ADisplayVolumeActor::ADisplayVolumeActor()
{
    PrimaryActorTick.bCanEverTick = false;

    VolumeBox = CreateDefaultSubobject<UBoxComponent>(TEXT("VolumeBox"));
    RootComponent = VolumeBox;

    // 100 cm half-extent → 200 cm full extent per axis.
    // Unreal normalises incoming geometry to ±100 cm, so scale is 1:1 by default.
    VolumeBox->SetBoxExtent(FVector(100.f, 100.f, 100.f));
    VolumeBox->SetCollisionEnabled(ECollisionEnabled::NoCollision);

    // Hide the outline during PIE — it's a placement guide only.
    VolumeBox->SetHiddenInGame(true);
    VolumeBox->ShapeColor = FColor(0, 200, 255);  // cyan wireframe in editor
}

#if WITH_EDITOR
void ADisplayVolumeActor::PostActorCreated()
{
    Super::PostActorCreated();

    UWorld* World = GetWorld();
    if (!World) return;

    // Collect names already in use by other DisplayVolumeActors.
    TSet<FString> UsedNames;
    TArray<AActor*> Existing;
    UGameplayStatics::GetAllActorsOfClass(World, StaticClass(), Existing);
    for (AActor* A : Existing)
    {
        if (ADisplayVolumeActor* V = Cast<ADisplayVolumeActor>(A))
            if (V != this)
                UsedNames.Add(V->VolumeName);
    }

    // Assign the lowest unused "Volume N" name (N ≥ 1).
    int32 N = 1;
    FString Candidate;
    do
    {
        Candidate = FString::Printf(TEXT("Volume %d"), N++);
    }
    while (UsedNames.Contains(Candidate));

    VolumeName = Candidate;
}
#endif
