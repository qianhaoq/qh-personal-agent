pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "QhPersonalAgent"
include(":app", ":data", ":core-security")
// Planned: :sync (Room/outbox), :feature-capture (audio), :feature-asr,
// :feature-entry (widgets/quick settings tile), :feature-reminder (notifications/scheduling).
