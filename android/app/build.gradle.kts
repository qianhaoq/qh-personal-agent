import java.util.Properties
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    kotlin("kapt")
}
val local = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}
val apiUrl = local.getProperty("api.baseUrl", "https://agent.pinhaoyun.com/")
require(apiUrl.endsWith("/") && (apiUrl.startsWith("https://") || apiUrl.startsWith("http://"))) {
    "api.baseUrl must be an http(s) URL ending in /"
}
val quotedUrl = "\"" + apiUrl.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
android {
    namespace = "dev.qh.agent"
    compileSdk = 34
    defaultConfig {
        applicationId = "dev.qh.agent"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
        buildConfigField("String", "API_BASE_URL", quotedUrl)
    }
    buildTypes {
        debug { buildConfigField("boolean", "USE_MOCK_API", (local.getProperty("api.useMock", "true") != "false").toString()) }
        release {
            buildConfigField("boolean", "USE_MOCK_API", "false")
            isMinifyEnabled = false
        }
    }
    testOptions { unitTests.isIncludeAndroidResources = true }
    buildFeatures { compose = true; buildConfig = true }
    composeOptions { kotlinCompilerExtensionVersion = "1.5.14" }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
}
dependencies {
    implementation(libs.retrofit.gson)
    implementation("androidx.room:room-runtime:2.6.1")
    kapt("androidx.room:room-compiler:2.6.1")
    testImplementation(libs.junit)
    testImplementation("org.robolectric:robolectric:4.13")
    testImplementation("androidx.test:core:1.6.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    testImplementation(platform(libs.compose.bom))
    testImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    implementation(project(":data")); implementation(project(":core-security"))
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui); implementation(libs.compose.material)
    implementation(libs.activity.compose); implementation(libs.navigation)
    implementation(libs.lifecycle); implementation(libs.coroutines.android)
}
