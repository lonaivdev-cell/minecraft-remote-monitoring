import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    // Target JVM 17 bytecode (matches the Android :app) without pinning a specific JDK,
    // so this module builds on whatever JDK 17+ runs the build — CI or a sandbox.
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    api(libs.kotlinx.coroutines.core)
    api(libs.kotlinx.serialization.json)

    // SSH transport. sshj is pure-JVM and works on Android; eddsa supplies the same
    // Ed25519 implementation sshj uses, and BouncyCastle the host-key primitives.
    // sshj is `api`: the app constructs the transport/verifier, so its public sshj types
    // (e.g. HostKeyVerifier) must be on the app's compile classpath.
    api(libs.sshj)
    implementation(libs.eddsa)
    implementation(libs.bouncycastle)
    implementation(libs.slf4j.api)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testRuntimeOnly(libs.slf4j.nop)
}

tasks.test {
    useJUnit()
    testLogging { events("passed", "skipped", "failed") }
}
