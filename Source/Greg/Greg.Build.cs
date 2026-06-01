// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;

public class Greg : ModuleRules
{
	public Greg(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[] {
			"Core",
			"CoreUObject",
			"Engine",
			"InputCore",
			"EnhancedInput",
			"AIModule",
			"StateTreeModule",
			"GameplayStateTreeModule",
			"UMG",
			"Slate",
			"Sockets",
			"Networking",
			"Json",
			"ProceduralMeshComponent"
		});

		PrivateDependencyModuleNames.AddRange(new string[] { });


		PublicIncludePaths.AddRange(new string[] {
			"Greg",
			"Greg/Variant_Platforming",
			"Greg/Variant_Platforming/Animation",
			"Greg/Variant_Combat",
			"Greg/Variant_Combat/AI",
			"Greg/Variant_Combat/Animation",
			"Greg/Variant_Combat/Gameplay",
			"Greg/Variant_Combat/Interfaces",
			"Greg/Variant_Combat/UI",
			"Greg/Variant_SideScrolling",
			"Greg/Variant_SideScrolling/AI",
			"Greg/Variant_SideScrolling/Gameplay",
			"Greg/Variant_SideScrolling/Interfaces",
			"Greg/Variant_SideScrolling/UI"
		});

		// Uncomment if you are using Slate UI
		// PrivateDependencyModuleNames.AddRange(new string[] { "Slate", "SlateCore" });

		// Uncomment if you are using online features
		// PrivateDependencyModuleNames.Add("OnlineSubsystem");

		// To include OnlineSubsystemSteam, add it to the plugins section in your uproject file with the Enabled attribute set to true
	}
}
