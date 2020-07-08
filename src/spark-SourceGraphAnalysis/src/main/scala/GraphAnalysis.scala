import org.apache.spark.sql.SparkSession
import org.apache.spark.{SparkConf, SparkContext}
import org.apache.spark.sql.functions._
import org.graphframes._
import org.apache.log4j.Logger
import org.apache.log4j.Level

//spark-shell --packages graphframes:graphframes:0.6.0-spark2.3-s_2.11
//--conf "spark.driver.extraJavaOptions=-Djava.io.tmpdir=tmp"


object GraphAnalysis {

  def main(args: Array[String]) {

    Logger.getLogger("org.apache.spark").setLevel(Level.ERROR)

    val spark = SparkSession.builder()
      .appName("GraphAnalysis").master("local[4]")
      .getOrCreate()

    val sc = SparkContext.getOrCreate()

    val lang = args(0)
    var nodes_path = args(1)
    var edges_path = args(2)

//    val lang = "python"

//    var nodes_path = ""
//    var edges_path = ""

//    if (lang == "python"){
//      /Volumes/External/datasets/Code/source-graphs/python-source-graph/
//      nodes_path = "normalized_sourcetrail_nodes.csv"
//      edges_path = "non-ambiguous_edges.csv"
//    } else if (lang == "java") {
//      /Volumes/External/datasets/Code/source-graphs/java-source-graph-v2
//      nodes_path = "normalized_sourcetrail_nodes.csv"
//      edges_path = "edge.csv"
//    }

    val nodes = spark.read.format("csv").option("header",true).load(nodes_path)
    val edges = spark.read.format("csv").option("header",true).load(edges_path).withColumnRenamed("source_node_id", "src").withColumnRenamed("target_node_id", "dst").withColumnRenamed("id", "rel_id").withColumnRenamed("type", "rel_type")

    val g = GraphFrame(nodes, edges)

    sc.setCheckpointDir("temp")
    val cc_result = g.connectedComponents.run()

    val g_cc = GraphFrame(cc_result, edges)

    var onlyConnected_g : GraphFrame = null

    if (lang == "python") {
      onlyConnected_g = g_cc.filterVertices("component == 0").dropIsolatedVertices() //.filterEdges("rel_type != \"4\"").dropIsolatedVertices()
    } else if (lang == "java") {
      onlyConnected_g = g_cc.filterVertices("component == 0").dropIsolatedVertices()
    }

    onlyConnected_g.vertices.write.format("csv").save("component_0_nodes")
    onlyConnected_g.edges.write.format("csv").save("component_0_edges")

    // val node_degres = nodes.join(g.inDegrees, "id")
    val node_inDegrees = onlyConnected_g.vertices.join(onlyConnected_g.inDegrees, "id")

    node_inDegrees.sort(desc("inDegree")).write.format("csv").save("component_0_node_in_degrees")
    node_inDegrees.groupBy("type").agg(mean("inDegree")).sort(desc("avg(inDegree)")).write.format("csv").save("component_0_node_avg_in_degree")
    node_inDegrees.groupBy("inDegree").count().sort(desc("count")).write.format("csv").save("component_0_in_degree_count") //  produces  |inDegree|count|

    // val node_outDegres = nodes.join(g.outDegrees, "id")
    val node_outDegrees = onlyConnected_g.vertices.join(onlyConnected_g.outDegrees, "id")

    node_outDegrees.sort(desc("outDegree")).write.format("csv").save("component_0_node_out_degrees")
    node_outDegrees.groupBy("type").agg(mean("outDegree")).sort(desc("avg(outDegree)")).write.format("csv").save("component_0_node_avg_out_degree")
    node_outDegrees.groupBy("outDegree").count().sort(desc("count")).write.format("csv").save("component_0_out_degree_count")

    onlyConnected_g.edges.groupBy("rel_type").count().write.format("csv").save("component_0_edge_type_count")
    onlyConnected_g.vertices.groupBy("type").count().write.format("csv").save("component_0_node_type_count")

//    val cc_result = g.connectedComponents.run()
//    cc_result.write.format("csv").save("components")
//    cc_result.groupBy("component").count().orderBy().write.format("csv").save("components_count")

//    val triangles = onlyConnected_g.triangleCount.run() // counts how many triangles pass through a given vertex
//    triangles.show(10)


//    val pr_results = g.pageRank.resetProbability(0.15).tol(0.01).run()

//    pr_results.vertices.write.format("csv").save("pagerank_vertices")

//    pr_results.edges.write.format("csv").save("pagerank_edges")

//    val textual_edges_path = "/Volumes/Untitled/normalized_sourcetrail_edges"
//    val textual_edges = spark.read.format("csv").option("header",true).load(textual_edges_path).withColumnRenamed("source_node_id", "src").withColumnRenamed("target_node_id", "dst").withColumnRenamed("id", "rel_id").withColumnRenamed("type", "rel_type")
//    textual_edges.filter("src == 'twisted.internet._glibbase.GlibReactorBase.__init__'").count

  }
}