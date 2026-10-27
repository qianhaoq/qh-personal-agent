plugins { alias(libs.plugins.kotlin.jvm) }
kotlin { jvmToolchain(17) }
dependencies {
    api(libs.retrofit); implementation(libs.retrofit.gson); implementation(libs.coroutines)
    testImplementation(libs.junit); testImplementation(libs.mockwebserver)
}
// Keep the monorepo CI task name usable for this platform-independent module.
tasks.register("testDebugUnitTest") { dependsOn("test") }
