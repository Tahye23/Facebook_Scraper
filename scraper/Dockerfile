# ─── Stage 1 : Build ─────────────────────────────────────────────────────────
FROM eclipse-temurin:21-jdk-alpine AS builder

WORKDIR /build

# Copier uniquement pom.xml + wrapper en premier pour profiter du cache Maven
COPY .mvn/ .mvn/
COPY mvnw pom.xml ./
RUN ./mvnw dependency:go-offline -q

# Copier les sources et builder le JAR
COPY src/ src/
RUN ./mvnw -q -DskipTests package

# ─── Stage 2 : Runtime ───────────────────────────────────────────────────────
FROM eclipse-temurin:21-jre-alpine AS runtime

WORKDIR /app

# Utilisateur non-root pour la production
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser

# Copier uniquement le JAR final
COPY --from=builder /build/target/*.jar app.jar

EXPOSE 8080

ENTRYPOINT ["java", "-jar", "app.jar"]
