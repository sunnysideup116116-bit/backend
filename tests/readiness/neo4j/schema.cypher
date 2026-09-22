// Only the contracts required by preference retrieval, in a disposable database.
CREATE CONSTRAINT concept_key IF NOT EXISTS
FOR (concept:Concept) REQUIRE concept.key IS UNIQUE;
CREATE CONSTRAINT readiness_user_id IF NOT EXISTS
FOR (user:User) REQUIRE user.id IS UNIQUE;
CREATE VECTOR INDEX concept_embedding_index IF NOT EXISTS
FOR (concept:Concept) ON (concept.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}};
