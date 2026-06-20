import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.compose.compiler)
}

// Version is driven by the release tag so the APK Android installs matches the
// GitHub release Obtainium shows. CI passes -PappVersionName / -PappVersionCode
// derived from the pushed tag (see .github/workflows/release.yml). Local/dev
// builds fall back to the defaults below.
//
// CRITICAL: versionCode must increase with every release, or Android/Obtainium
// treats the new APK as "already installed" and refuses to upgrade. The release
// workflow computes it from semver as major*10000 + minor*100 + patch.
val appVersionName = (project.findProperty("appVersionName") as String?) ?: "0.6.0"
val appVersionCode = (project.findProperty("appVersionCode") as String?)?.toIntOrNull() ?: 1

android {
    namespace = "com.carborioland.mcctl"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.carborioland.mcctl"
        minSdk = 26
        targetSdk = 35
        versionCode = appVersionCode
        versionName = appVersionName
        vectorDrawables { useSupportLibrary = true }
    }

    buildTypes {
        getByName("debug") {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        getByName("release") {
            // Keep R8 off for now: sshj + BouncyCastle + eddsa need careful keep rules,
            // and a guaranteed-working APK matters more than size for this companion app.
            // (Re-enable with proguard-rules.pro once the keeps are dialed in.)
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            // sshj/BouncyCastle/eddsa ship signed jars + license noise that collide when
            // merged into one APK. Strip the signatures and docs; KEEP META-INF/services
            // (sshj discovers its crypto factories there).
            excludes += setOf(
                "META-INF/INDEX.LIST",
                "META-INF/DEPENDENCIES",
                "META-INF/{LICENSE,LICENSE.txt,LICENSE.md,LICENSE-notice.md,license.txt}",
                "META-INF/{NOTICE,NOTICE.txt,NOTICE.md,notice.txt}",
                "META-INF/*.SF",
                "META-INF/*.DSA",
                "META-INF/*.RSA",
                "META-INF/*.EC",
                "META-INF/versions/9/OSGI-INF/MANIFEST.MF",
            )
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(project(":core"))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.androidx.material.icons.extended)
    implementation(libs.androidx.navigation.compose)

    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.security.crypto)
    implementation(libs.androidx.biometric)
    implementation(libs.androidx.work.runtime.ktx)

    debugImplementation(libs.androidx.ui.tooling)
}
