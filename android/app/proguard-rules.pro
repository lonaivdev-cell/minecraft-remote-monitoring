# R8/shrinking is disabled for the release build today (see app/build.gradle.kts), so
# these rules are inert. They are kept here so that turning shrinking on later is a
# one-line change, not a debugging session:
#
#   - sshj discovers ciphers/KEX/signature factories reflectively via META-INF/services
#     and class names, so its packages and BouncyCastle must be kept.
#   - net.i2p.crypto.eddsa provides the Ed25519 key types sshj checks by class.
#
# -keep class net.schmizz.sshj.** { *; }
# -keep class com.hierynomus.** { *; }
# -keep class org.bouncycastle.** { *; }
# -keep class net.i2p.crypto.eddsa.** { *; }
# -dontwarn org.slf4j.**
# -dontwarn org.bouncycastle.**
