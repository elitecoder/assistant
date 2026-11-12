#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Runs `block` and returns the Objective-C exception it raised, if any. AVFoundation reports some
/// failures, such as an invalid audio tap format, by raising instead of returning an error.
NSException *_Nullable AssistantCatchException(NS_NOESCAPE void (^block)(void));

NS_ASSUME_NONNULL_END
