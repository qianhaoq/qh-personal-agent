package dev.qh.agent

import android.content.Context
import androidx.room.*
import com.google.gson.Gson
import dev.qh.data.*

@Entity(tableName = "composer")
data class ComposerRow(@PrimaryKey val ownerKey: String, val epoch: String, val payload: String)
@Dao
interface ComposerDao {
    @Query("SELECT * FROM composer WHERE ownerKey = :key AND epoch = :epoch")
    fun read(key: String, epoch: String): ComposerRow?
    @Insert(onConflict = OnConflictStrategy.REPLACE) fun write(row: ComposerRow)
    @Query("DELETE FROM composer") fun clear()
}
@Database(entities = [ComposerRow::class], version = 1, exportSchema = false)
abstract class ComposerDatabase : RoomDatabase() { abstract fun composer(): ComposerDao }

/** Small synchronous transactions intentionally stay inside Repository.withCurrentOwner.
 * No credentials, answers, notes or reminder projections are written here.
 */
class RoomComposerStore(context: Context, databaseName: String = "composer-recovery.db") : ComposerStore, AutoCloseable {
    private val db = Room.databaseBuilder(context.applicationContext, ComposerDatabase::class.java,
        databaseName).allowMainThreadQueries().build()
    private val gson = Gson()
    private fun key(owner: RequestOwner) = gson.toJson(listOf(owner.runtimeKey, owner.userId))
    override fun load(owner: RequestOwner): ComposerState = db.composer().read(key(owner), owner.loginEpoch)
        ?.let { gson.fromJson(it.payload, ComposerState::class.java) } ?: ComposerState()
    override fun save(owner: RequestOwner, state: ComposerState) {
        db.runInTransaction {
            db.composer().write(ComposerRow(key(owner), owner.loginEpoch, gson.toJson(state)))
        }
    }
    override fun close() = db.close()
    override fun clear() = db.runInTransaction { db.composer().clear() }
}
